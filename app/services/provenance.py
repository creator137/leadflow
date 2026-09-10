from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, CompanyFieldProvenance


METHOD_PRIORITY = {
    "ai_website_analysis": 10,
    "parser": 20,
    "website_regex": 30,
    "website_structured_data": 40,
    "manual": 100,
}


def latest_provenance(session: Session, company_id: str, field: str) -> CompanyFieldProvenance | None:
    return session.scalar(
        select(CompanyFieldProvenance)
        .where(CompanyFieldProvenance.company_id == company_id, CompanyFieldProvenance.field == field)
        .order_by(CompanyFieldProvenance.discovered_at.desc())
        .limit(1)
    )


def apply_field(
    session: Session,
    company: Company,
    field: str,
    value: Any,
    *,
    discovery_method: str,
    source_url: str | None = None,
    confidence: float | None = None,
) -> bool:
    """Apply a value only when its ownership outranks the current provenance."""
    if value is None or value == "":
        return False
    if field == "website":
        from app.normalize import normalize_website
        value = normalize_website(str(value))
        if not value:
            return False
    previous = latest_provenance(session, company.id, field)
    incoming_priority = METHOD_PRIORITY.get(discovery_method, 0)
    previous_priority = METHOD_PRIORITY.get(previous.discovery_method, 0) if previous else -1
    current = getattr(company, field)
    if previous and previous_priority > incoming_priority:
        return False
    if previous and previous_priority == incoming_priority and discovery_method != "manual" and current not in (None, "") and current != value:
        return False
    setattr(company, field, value)
    if field == "company_email":
        company.email = value
    elif field == "company_phone":
        company.phone = value
    elif field == "decision_maker_name":
        company.contact_person = value
    elif field == "website":
        from app.normalize import website_domain
        company.website_domain = website_domain(value)
    session.add(CompanyFieldProvenance(
        company_id=company.id,
        field=field,
        value=str(value),
        source_url=source_url,
        confidence=confidence,
        discovery_method=discovery_method,
        discovered_at=datetime.now(timezone.utc),
    ))
    return current != value
