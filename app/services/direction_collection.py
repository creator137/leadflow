from __future__ import annotations

import math
import time
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

    locations = list(session.scalars(select(DirectionLocation).where(
        DirectionLocation.direction_id == direction.id, DirectionLocation.active.is_(True),
    ).order_by(DirectionLocation.city)))
    queries = list(session.scalars(select(DirectionSearchQuery).where(
        DirectionSearchQuery.direction_id == direction.id, DirectionSearchQuery.active.is_(True),
    ).order_by(DirectionSearchQuery.priority, DirectionSearchQuery.created_at)))
    sources = list(session.scalars(select(DirectionSource).where(
        DirectionSource.direction_id == direction.id, DirectionSource.active.is_(True),
    ).order_by(DirectionSource.source)))
    combinations = [(location, query, source) for location in locations for query in queries for source in sources]
    start_combo = int((run.checkpoint or {}).get("combo_index", 0))
    resume_external_id = (run.checkpoint or {}).get("last_external_id")
    resume_source_url = (run.checkpoint or {}).get("last_source_url")
    blocked = failed = 0

    for combo_index, (location, query, source) in enumerate(combinations):
        if combo_index < start_combo:
            continue
        remaining = run.limit_new - run.inserted
        if remaining <= 0:
            break
        spec = SearchSpec(
            category=query.query,
            city=location.city,
            limit=max(remaining, min(settings.source_max_scan, max(30, remaining * 5))),
            # A direction combines multiple queries/sources; the bounded list
            # search is preferable to the city-wide grid used by exhaustive
            # one-off Parser Core jobs.
            options={"enrich_emails": direction.email_enrichment_enabled, "use_grid": False},
        )
        adapter = build_adapter(source.source, settings)
        inserted_before_combo = run.inserted
        combo_target = max(1, math.ceil(remaining / (len(combinations) - combo_index)))
        resuming = combo_index == start_combo and bool(resume_external_id or resume_source_url)
        combo_completed = False
        combo_started = time.monotonic()
        try:
            for lead in adapter.collect(spec):
                if resuming:
                    reached = (
                        (resume_external_id and lead.source_external_id == resume_external_id)
                        or (resume_source_url and lead.source_url == resume_source_url)
                    )
                    if reached:
                        resuming = False
                    continue
                if not lead.company_name or lead.company_name == "Unknown name":
                    continue
                run.scanned += 1
                result = upsert_lead(session, lead)
                _parser_provenance(session, result.company, lead, location.region)
                _source_record(session, result.company.id, lead, query.query, location.city)
                association = session.scalar(select(CompanyDirection).where(
                    CompanyDirection.company_id == result.company.id,
                    CompanyDirection.direction_id == direction.id,
                ))
                if association:
                    run.duplicates += 1
                else:
                    session.add(CompanyDirection(company_id=result.company.id, direction_id=direction.id))
                    run.inserted += 1
                session.add(SearchObservation(
                    run_id=run.id, direction_id=direction.id, company_id=result.company.id,
                    source=lead.source, query=query.query, city=location.city,
                    source_external_id=lead.source_external_id, source_url=lead.source_url,
                    is_new=not bool(association), matched_by=result.matched_by,
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
                session.commit()
                if run.inserted >= run.limit_new or run.inserted - inserted_before_combo >= combo_target:
                    break
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
