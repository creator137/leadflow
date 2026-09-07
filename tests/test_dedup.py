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

