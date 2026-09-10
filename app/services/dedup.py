from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company
from app.normalize import name_address_fingerprint, normalize_phone, normalize_text, normalize_website, website_domain
from app.sources.base import CompanyLead


@dataclass(slots=True)
class UpsertResult:
    company: Company
    inserted: bool
    matched_by: str | None = None


def _merge_enrichment(session: Session, company: Company, lead: CompanyLead) -> None:
    if not company.email and lead.email:
        company.email = lead.email
        company.company_email = lead.email
    phone = normalize_phone(lead.phone)
    phone_owner = session.scalar(select(Company.id).where(Company.normalized_phone == phone, Company.id != company.id).limit(1)) if phone else None
    if not company.company_phone and lead.phone and not phone_owner:
        company.company_phone = lead.phone
        company.phone = lead.phone
        company.normalized_phone = phone
    if not company.website and lead.website:
        normalized_site = normalize_website(lead.website)
        domain = website_domain(normalized_site)
        domain_owner = session.scalar(select(Company.id).where(Company.website_domain == domain, Company.id != company.id).limit(1)) if domain else None
        if not domain_owner:
            company.website = normalized_site
            company.website_domain = domain
    if company.branches_count is None and lead.branches_count is not None:
        company.branches_count = lead.branches_count
    discovery = (lead.raw_data or {}).get("email_discovery")
    if discovery:
        company.raw_data = {**(company.raw_data or {}), "email_discovery": discovery}


def find_duplicate(session: Session, lead: CompanyLead) -> tuple[Company | None, str | None]:
    domain = website_domain(lead.website)
    phone = normalize_phone(lead.phone)
    fingerprint = name_address_fingerprint(lead.company_name, lead.address)
    if lead.source_external_id:
        company = session.scalar(select(Company).where(Company.source == lead.source, Company.source_external_id == lead.source_external_id).limit(1))
        if company: return company, "source_external_id"
    if domain:
        company = session.scalar(select(Company).where(Company.website_domain == domain).limit(1))
        if company: return company, "website_domain"
    if phone:
        company = session.scalar(select(Company).where(Company.normalized_phone == phone).limit(1))
        if company: return company, "phone"
    if lead.inn:
        company = session.scalar(select(Company).where(Company.inn == lead.inn).limit(1))
        if company: return company, "inn"
    if fingerprint:
        company = session.scalar(select(Company).where(Company.name_address_fingerprint == fingerprint).limit(1))
        if company: return company, "name_address"
    return None, None


def upsert_lead(session: Session, lead: CompanyLead) -> UpsertResult:
    duplicate, matched_by = find_duplicate(session, lead)
    if duplicate:
        _merge_enrichment(session, duplicate, lead)
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
        website=normalize_website(lead.website),
        branches_count=lead.branches_count,
        inn=lead.inn,
        contact_person=lead.contact_person,
        decision_maker_name=lead.contact_person,
        collected_at=lead.collected_at,
        raw_data=lead.raw_data,
        normalized_name=normalize_text(lead.company_name) or "",
        normalized_address=normalize_text(lead.address),
        normalized_phone=normalize_phone(lead.phone),
        website_domain=website_domain(normalize_website(lead.website)),
        name_address_fingerprint=name_address_fingerprint(lead.company_name, lead.address),
    )
    try:
        with session.begin_nested():
            session.add(company)
            session.flush()
    except IntegrityError:
        duplicate, matched_by = find_duplicate(session, lead)
        if duplicate:
            _merge_enrichment(session, duplicate, lead)
            return UpsertResult(duplicate, False, matched_by or "unique_constraint")
        raise
    return UpsertResult(company, True)
