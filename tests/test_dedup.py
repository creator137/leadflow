from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.services.dedup import upsert_lead
from app.sources.base import CompanyLead


def test_deduplicates_by_phone_and_domain() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = CompanyLead(
            source="yandex_maps",
            source_external_id="1",
            source_url="https://maps/1",
            company_name="ООО Ромашка",
            address="Ленина, 1",
            phone="+7 999 123-45-67",
            website="https://www.romashka.ru/contacts",
        )
        second = CompanyLead(
            source="two_gis",
            source_external_id="2",
            source_url="https://maps/2",
            company_name="Ромашка",
            address="ул. Ленина, 1",
            phone="8 (999) 123-45-67",
            website="http://romashka.ru",
        )
        assert upsert_lead(session, first).inserted is True
        result = upsert_lead(session, second)
        assert result.inserted is False
        assert result.matched_by in {"phone", "website_domain"}


def test_duplicate_merges_discovered_email() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        original = CompanyLead(
            source="two_gis", source_external_id="email-1", source_url="https://2gis/1",
            company_name="Храм", website="https://example.org",
        )
        enriched = CompanyLead(
            source="two_gis", source_external_id="email-1", source_url="https://2gis/1",
            company_name="Храм", website="https://example.org", email="info@example.org",
            raw_data={"email_discovery": [{"email": "info@example.org", "page_url": "https://example.org/contacts"}]},
        )
        company = upsert_lead(session, original).company
        result = upsert_lead(session, enriched)
        assert result.inserted is False
        assert company.email == "info@example.org"
        assert company.raw_data["email_discovery"][0]["page_url"].endswith("/contacts")
