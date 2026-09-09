from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIConfig, Company, CompanyDirection, Direction, DirectionRun
from app.services.company_enrichment import CompanyEnrichmentService
from app.services.direction_collection import execute_direction
from app.services.google_sheets import GoogleSheetsSyncService, active_sheets_config


def execute_direction_pipeline(
    session: Session, direction: Direction, settings: Settings, run: DirectionRun | None = None,
) -> DirectionRun:
    run = execute_direction(session, direction, settings, run)
    if direction.email_enrichment_enabled or direction.ai_enrichment_enabled:
        ai_config = None
        if direction.ai_enrichment_enabled:
            ai_config = session.scalar(select(AIConfig).where(AIConfig.active.is_(True)).limit(1))
        companies = list(session.scalars(
            select(Company).join(CompanyDirection).where(
                CompanyDirection.direction_id == direction.id,
                CompanyDirection.created_at >= run.started_at,
            )
        ))
        service = CompanyEnrichmentService(session)
        for company in companies:
            service.enrich(company, ai_config=ai_config)
    config = active_sheets_config(session, settings)
    if config:
        GoogleSheetsSyncService(session, config).sync_direction(direction)
    return run
