from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    CompanyDirection, CompanySourceRecord, Direction, DirectionLocation, DirectionRun,
    DirectionSearchQuery, DirectionSource, SearchObservation,
)
from app.services.dedup import upsert_lead
from app.services.provenance import apply_field
from app.services.region_catalog import cities_for_region
from app.sources.base import SearchSpec, SourceBlocked
from app.sources.factory import build_adapter
from app.sources.two_gis.adapter import TwoGisAdapter


def _source_record(session: Session, company_id: str, lead, query: str, city: str) -> None:
    exists = None
    if lead.source_external_id:
        exists = session.scalar(select(CompanySourceRecord.id).where(
            CompanySourceRecord.source == lead.source,
            CompanySourceRecord.source_external_id == lead.source_external_id,
        ))
    elif lead.source_url:
        exists = session.scalar(select(CompanySourceRecord.id).where(
            CompanySourceRecord.source == lead.source,
            CompanySourceRecord.source_url == lead.source_url,
            CompanySourceRecord.company_id == company_id,
        ))
    if not exists:
        session.add(CompanySourceRecord(
            company_id=company_id,
            source=lead.source,
            source_external_id=lead.source_external_id,
            source_url=lead.source_url,
            query=query,
            city=city,
            raw_data=lead.raw_data or {},
            collected_at=lead.collected_at,
        ))


def _parser_provenance(session: Session, company, lead, region: str | None) -> None:
    for field, value in (
        ("region", region),
        ("company_email", lead.email),
        ("company_phone", company.company_phone if lead.phone else None),
        ("inn", company.inn if lead.inn else None),
        ("website", company.website if lead.website else None),
        ("branches_count", lead.branches_count),
    ):
        if value:
            apply_field(
                session, company, field, value,
                discovery_method="parser", source_url=lead.source_url, confidence=1.0,
            )


@dataclass(frozen=True)
class SearchLocation:
    """One concrete city query, derived from a city or a saved region."""
    key: str
    city: str
    region: str | None


def expand_locations(locations: list[DirectionLocation]) -> list[SearchLocation]:
    """Turn regional settings into source-safe city queries.

    The saved row remains one human-readable region setting. Expansion happens
    only while a run is executing, so Google Sheets still receives the actual
    city returned by each source and no extra direction settings are created.
    """
    expanded: list[SearchLocation] = []
    for location in locations:
        if location.scope == "region":
            region = location.region or location.city
            expanded.extend(
                SearchLocation(key=f"{location.id}:{city}", city=city, region=region)
                for city in cities_for_region(region)
            )
        else:
            expanded.append(SearchLocation(key=location.id, city=location.city, region=location.region))
    return expanded


def execute_direction(
    session: Session,
    direction: Direction,
    settings: Settings,
    run: DirectionRun | None = None,
) -> DirectionRun:
    run = run or DirectionRun(direction_id=direction.id, limit_new=direction.limit_new)
    session.add(run)
    session.commit()
    run.status = "running"
    run.finished_at = None
    session.commit()

    saved_locations = list(session.scalars(select(DirectionLocation).where(
        DirectionLocation.direction_id == direction.id, DirectionLocation.active.is_(True),
    ).order_by(DirectionLocation.city)))
    # Search explicitly requested cities before expanding the surrounding
    # region. A large region must not consume most of a daily run before the
    # main city is reached.
    saved_locations.sort(key=lambda location: location.scope == "region")
    locations = expand_locations(saved_locations)
    queries = list(session.scalars(select(DirectionSearchQuery).where(
        DirectionSearchQuery.direction_id == direction.id, DirectionSearchQuery.active.is_(True),
    ).order_by(DirectionSearchQuery.priority, DirectionSearchQuery.created_at)))
    sources = list(session.scalars(select(DirectionSource).where(
        DirectionSource.direction_id == direction.id, DirectionSource.active.is_(True),
    ).order_by(DirectionSource.source)))
    unsupported_two_gis_cities = (
        {location.city for location in locations if not TwoGisAdapter.supports_city(location.city)}
        if any(source.source == "two_gis" for source in sources) else set()
    )
    combinations = [
        (location, query, source)
        for location in locations for query in queries for source in sources
        if source.source != "two_gis" or location.city not in unsupported_two_gis_cities
    ]
    search_cursor = dict(direction.search_cursor or {})
    known_source_ids = {
        (source, external_id)
        for source, external_id in session.execute(
            select(CompanySourceRecord.source, CompanySourceRecord.source_external_id)
            .join(CompanyDirection, CompanyDirection.company_id == CompanySourceRecord.company_id)
            .where(CompanyDirection.direction_id == direction.id, CompanySourceRecord.source_external_id.is_not(None))
        )
    }
    start_combo = int((run.checkpoint or {}).get("combo_index", 0))
    blocked = failed = 0

    for combo_index, (location, query, source) in enumerate(combinations):
        if combo_index < start_combo:
            continue
        remaining = run.limit_new - run.inserted
        if remaining <= 0:
            break
        cursor_key = f"{location.key}:{query.id}:{source.id}"
        cursor = dict(search_cursor.get(cursor_key) or {})
        cursor_offset = max(0, int(cursor.get("offset", 0)))
        paged = source.source == "yandex_maps"
        exhausted_at = None
        if paged and cursor.get("exhausted_at"):
            try:
                exhausted_at = datetime.fromisoformat(cursor["exhausted_at"])
            except (TypeError, ValueError):
                pass
        probe_only = bool(exhausted_at and datetime.now(timezone.utc) - exhausted_at < timedelta(days=7))
        # Popular cities can have several pages of already known cards. Search
        # far enough in one run to reach new companies rather than advancing
        # only a couple of pages per day.
        scan_step = max(150, remaining * 5) if paged else max(30, remaining * 5)
        scan_limit = min(settings.source_max_scan, 25 if probe_only else (scan_step if paged else max(remaining, cursor_offset + scan_step)))
        options = {"enrich_emails": direction.email_enrichment_enabled, "use_grid": False}
        if paged:
            options.update({
                "start_page": 1 if probe_only or exhausted_at else max(1, int(cursor.get("page", 1))),
                "start_index": 0 if probe_only or exhausted_at else max(0, int(cursor.get("index", 0))),
                "max_pages": 1 if probe_only else 20,
            })
        spec = SearchSpec(
            category=query.query,
            city=location.city,
            limit=scan_limit,
            # A direction combines multiple queries/sources; the bounded list
            # search is preferable to the city-wide grid used by exhaustive
            # one-off Parser Core jobs.
            options=options,
        )
        adapter = build_adapter(source.source, settings)
        inserted_before_combo = run.inserted
        combo_target = max(1, math.ceil(remaining / (len(combinations) - combo_index)))
        combo_completed = False
        combo_started = time.monotonic()
        yielded = 0
        stopped_early = False
        try:
            for yielded, lead in enumerate(adapter.collect(spec), start=1):
                if not paged and yielded <= cursor_offset:
                    continue
                if paged and lead.source_page is not None and lead.source_index is not None:
                    next_page = lead.source_page + int(lead.source_index + 1 >= (lead.source_page_size or 1))
                    next_index = 0 if next_page > lead.source_page else lead.source_index + 1
                    search_cursor[cursor_key] = {"version": 2, "page": next_page, "index": next_index}
                    if probe_only:
                        search_cursor[cursor_key]["exhausted_at"] = cursor["exhausted_at"]
                    direction.search_cursor = dict(search_cursor)
                if lead.source_external_id and (source.source, lead.source_external_id) in known_source_ids:
                    continue
                if not lead.company_name or lead.company_name == "Unknown name":
                    continue
                # Adapters normally return the city from the card itself. If a
                # source omits it, retain the concrete city that was queried —
                # never the umbrella region — so the Company and its Sheet row
                # remain understandable to the administrator.
                if not lead.city:
                    lead.city = location.city
                run.scanned += 1
                result = upsert_lead(session, lead)
                _parser_provenance(session, result.company, lead, location.region)
                _source_record(session, result.company.id, lead, query.query, location.city)
                if lead.source_external_id:
                    known_source_ids.add((source.source, lead.source_external_id))
                already_linked = session.scalar(select(CompanyDirection.id).where(
                    CompanyDirection.company_id == result.company.id,
                    CompanyDirection.direction_id == direction.id,
                ))
                newly_linked = not bool(already_linked)
                if newly_linked:
                    session.add(CompanyDirection(company_id=result.company.id, direction_id=direction.id))
                    run.inserted += 1
                else:
                    run.duplicates += 1
                session.add(SearchObservation(
                    run_id=run.id, direction_id=direction.id, company_id=result.company.id,
                    source=lead.source, query=query.query, city=location.city,
                    source_external_id=lead.source_external_id, source_url=lead.source_url,
                    is_new=newly_linked, matched_by=result.matched_by,
                    has_phone=bool(lead.phone), has_email=bool(lead.email),
                    has_website=bool(lead.website), has_branches_count=lead.branches_count is not None,
                    duration_ms=int((time.monotonic() - combo_started) * 1000),
                ))
                run.checkpoint = {
                    "combo_index": combo_index,
                    "last_external_id": lead.source_external_id,
                    "last_source_url": lead.source_url,
                    "query": query.query,
                    "city": location.city,
                    "source": source.source,
                }
                if not paged or lead.source_page is None:
                    search_cursor[cursor_key] = {
                        "offset": yielded,
                        "last_external_id": lead.source_external_id,
                        "last_source_url": lead.source_url,
                    }
                    direction.search_cursor = dict(search_cursor)
                session.commit()
                if run.inserted >= run.limit_new or run.inserted - inserted_before_combo >= combo_target:
                    stopped_early = True
                    break
            if not stopped_early:
                if paged and spec.options.get("_source_exhausted"):
                    search_cursor[cursor_key] = {
                        "version": 2, "page": 1, "index": 0,
                        "exhausted_at": datetime.now(timezone.utc).isoformat(),
                    }
                    direction.search_cursor = dict(search_cursor)
                elif not paged and (yielded < scan_limit or yielded >= settings.source_max_scan):
                    search_cursor[cursor_key] = {"offset": 0}
                    direction.search_cursor = dict(search_cursor)
            combo_completed = run.inserted < run.limit_new
        except SourceBlocked as exc:
            session.rollback()
            run = session.get(DirectionRun, run.id)
            blocked += 1
            run.errors += 1
            run.message = str(exc)
            session.add(SearchObservation(
                run_id=run.id, direction_id=direction.id, source=source.source,
                query=query.query, city=location.city, is_new=False,
                error_type=exc.__class__.__name__, error_message=str(exc),
                duration_ms=int((time.monotonic() - combo_started) * 1000),
            ))
        except Exception as exc:
            session.rollback()
            run = session.get(DirectionRun, run.id)
            failed += 1
            run.errors += 1
            run.message = str(exc)
            session.add(SearchObservation(
                run_id=run.id, direction_id=direction.id, source=source.source,
                query=query.query, city=location.city, is_new=False,
                error_type=exc.__class__.__name__, error_message=str(exc),
                duration_ms=int((time.monotonic() - combo_started) * 1000),
            ))
        if combo_completed:
            run.checkpoint = {"combo_index": combo_index + 1}
        session.commit()

    if run.inserted >= run.limit_new:
        run.status = "completed"
    elif combinations and blocked == len(combinations):
        run.status = "blocked"
    elif combinations and failed == len(combinations):
        run.status = "failed"
    else:
        run.status = "exhausted"
    if unsupported_two_gis_cities and not run.message:
        run.message = f"2ГИС: городов без поддержки в каталоге источника — {len(unsupported_two_gis_cities)}."
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run
