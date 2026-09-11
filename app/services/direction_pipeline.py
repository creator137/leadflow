from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Company, CompanyDirection, Direction, DirectionRun
from app.services.company_enrichment import CompanyEnrichmentService
from app.services.direction_collection import execute_direction
from app.services.google_sheets import GoogleSheetsSyncService, active_sheets_config


def execute_direction_pipeline(
    session: Session, direction: Direction, settings: Settings, run: DirectionRun | None = None,
) -> DirectionRun:
    run = execute_direction(session, direction, settings, run)
    if direction.email_enrichment_enabled or direction.ai_enrichment_enabled:
        companies = list(session.scalars(
            select(Company).join(CompanyDirection).where(
                CompanyDirection.direction_id == direction.id,
                CompanyDirection.created_at >= run.started_at,
            )
        ))
        service = CompanyEnrichmentService(session, settings)
        for company in companies:
            service.enrich(company, use_ai=direction.ai_enrichment_enabled)
    config = active_sheets_config(session, settings)
    if config:
        GoogleSheetsSyncService(session, config).sync_direction(direction)
    return run
