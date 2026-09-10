from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, CompanyFieldProvenance, CompanySourceRecord
from app.normalize import normalize_website, website_domain
from app.services.provenance import apply_field


def backfill_structured_business_fields(session: Session) -> int:
    """Normalize legacy websites and recover verified parser branch counts."""
    changed = 0
    for company in session.scalars(select(Company).where(Company.website.is_not(None))):
        normalized = normalize_website(company.website)
        domain = website_domain(normalized)
        if company.website != normalized or company.website_domain != domain:
            company.website = normalized
            company.website_domain = domain
            changed += 1
    records = session.scalars(select(CompanySourceRecord).order_by(CompanySourceRecord.collected_at.desc()))
    for record in records:
        company = session.get(Company, record.company_id)
        if not company or company.branches_count is not None:
            continue
        raw = record.raw_data or {}
        value = raw.get("branches_count")
        if record.source == "two_gis":
            value = (raw.get("org") or {}).get("branch_count")
        if not isinstance(value, int) or value < 1:
            continue
        has_provenance = session.scalar(select(CompanyFieldProvenance.id).where(
            CompanyFieldProvenance.company_id == company.id,
            CompanyFieldProvenance.field == "branches_count",
        ).limit(1))
        if not has_provenance:
            changed += int(apply_field(session, company, "branches_count", value,
                                       discovery_method="parser", source_url=record.source_url, confidence=1.0))
    session.commit()
    return changed
