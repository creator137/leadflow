from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company
from app.normalize import name_address_fingerprint, normalize_phone, normalize_text, website_domain
from app.sources.base import CompanyLead


@dataclass(slots=True)
class UpsertResult:
    company: Company
    inserted: bool
    matched_by: str | None = None


def _merge_enrichment(company: Company, lead: CompanyLead) -> None:
    if not company.email and lead.email:
        company.email = lead.email
        company.company_email = lead.email
    if not company.company_phone and lead.phone:
        company.company_phone = lead.phone
    discovery = (lead.raw_data or {}).get("email_discovery")
    if discovery:
        company.raw_data = {**(company.raw_data or {}), "email_discovery": discovery}


def find_duplicate(session: Session, lead: CompanyLead) -> tuple[Company | None, str | None]:
    domain = website_domain(lead.website)
    phone = normalize_phone(lead.phone)
    fingerprint = name_address_fingerprint(lead.company_name, lead.address)
    clauses = []
    labels = []
    if lead.source_external_id:
        clauses.append((Company.source == lead.source) & (Company.source_external_id == lead.source_external_id))
        labels.append("source_external_id")
    if domain:
        clauses.append(Company.website_domain == domain)
        labels.append("website_domain")
    if phone:
        clauses.append(Company.normalized_phone == phone)
        labels.append("phone")
    if lead.inn:
        clauses.append(Company.inn == lead.inn)
        labels.append("inn")
    if fingerprint:
        clauses.append(Company.name_address_fingerprint == fingerprint)
        labels.append("name_address")
    if not clauses:
        return None, None
    company = session.scalar(select(Company).where(or_(*clauses)).limit(1))
    if not company:
        return None, None
    if lead.source_external_id and company.source == lead.source and company.source_external_id == lead.source_external_id:
        return company, "source_external_id"
    if domain and company.website_domain == domain:
        return company, "website_domain"
    if phone and company.normalized_phone == phone:
        return company, "phone"
    if lead.inn and company.inn == lead.inn:
        return company, "inn"
    return company, labels[-1]


def upsert_lead(session: Session, lead: CompanyLead) -> UpsertResult:
    duplicate, matched_by = find_duplicate(session, lead)
    if duplicate:
        _merge_enrichment(duplicate, lead)
        return UpsertResult(duplicate, False, matched_by)

    company = Company(
        source=lead.source,
        source_external_id=lead.source_external_id,
        source_url=lead.source_url,
        company_name=lead.company_name,
        category=lead.category,
        city=lead.city,
        address=lead.address,
        phone=lead.phone,
        email=lead.email,
        company_phone=lead.phone,
        company_email=lead.email,
        website=lead.website,
        inn=lead.inn,
        contact_person=lead.contact_person,
        decision_maker_name=lead.contact_person,
        collected_at=lead.collected_at,
        raw_data=lead.raw_data,
        normalized_name=normalize_text(lead.company_name) or "",
        normalized_address=normalize_text(lead.address),
        normalized_phone=normalize_phone(lead.phone),
        website_domain=website_domain(lead.website),
        name_address_fingerprint=name_address_fingerprint(lead.company_name, lead.address),
    )
    try:
        with session.begin_nested():
            session.add(company)
            session.flush()
    except IntegrityError:
        duplicate, matched_by = find_duplicate(session, lead)
        if duplicate:
            _merge_enrichment(duplicate, lead)
            return UpsertResult(duplicate, False, matched_by or "unique_constraint")
        raise
    return UpsertResult(company, True)
