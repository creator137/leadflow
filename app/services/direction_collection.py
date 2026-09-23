from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

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
    locations = expand_locations(saved_locations)
    queries = list(session.scalars(select(DirectionSearchQuery).where(
        DirectionSearchQuery.direction_id == direction.id, DirectionSearchQuery.active.is_(True),
    ).order_by(DirectionSearchQuery.priority, DirectionSearchQuery.created_at)))
    sources = list(session.scalars(select(DirectionSource).where(
        DirectionSource.direction_id == direction.id, DirectionSource.active.is_(True),
    ).order_by(DirectionSource.source)))
    combinations = [(location, query, source) for location in locations for query in queries for source in sources]
    search_cursor = dict(direction.search_cursor or {})
    start_combo = int((run.checkpoint or {}).get("combo_index", 0))
    blocked = failed = 0

    for combo_index, (location, query, source) in enumerate(combinations):
        if combo_index < start_combo:
            continue
        remaining = run.limit_new - run.inserted
        if remaining <= 0:
            break
        cursor_key = f"{location.key}:{query.id}:{source.id}"
        cursor_offset = max(0, int((search_cursor.get(cursor_key) or {}).get("offset", 0)))
        scan_step = max(30, remaining * 5)
        scan_limit = min(settings.source_max_scan, max(remaining, cursor_offset + scan_step))
        spec = SearchSpec(
            category=query.query,
            city=location.city,
            limit=scan_limit,
            # A direction combines multiple queries/sources; the bounded list
            # search is preferable to the city-wide grid used by exhaustive
            # one-off Parser Core jobs.
            options={"enrich_emails": direction.email_enrichment_enabled, "use_grid": False},
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
                if yielded <= cursor_offset:
                    continue
                if not lead.company_name or lead.company_name == "Unknown name":
                    continue
                run.scanned += 1
                result = upsert_lead(session, lead)
                _parser_provenance(session, result.company, lead, location.region)
                _source_record(session, result.company.id, lead, query.query, location.city)
                if result.inserted:
                    session.add(CompanyDirection(company_id=result.company.id, direction_id=direction.id))
                    run.inserted += 1
                else:
                    run.duplicates += 1
                session.add(SearchObservation(
                    run_id=run.id, direction_id=direction.id, company_id=result.company.id,
                    source=lead.source, query=query.query, city=location.city,
                    source_external_id=lead.source_external_id, source_url=lead.source_url,
                    is_new=result.inserted, matched_by=result.matched_by,
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
                # The current result window was exhausted. Start from the top
                # on the next day so newly appeared high-ranking cards are not missed.
                if yielded < scan_limit or yielded >= settings.source_max_scan:
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
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run
