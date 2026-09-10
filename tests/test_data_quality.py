from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Company
from app.services.data_quality import backfill_structured_business_fields


def test_legacy_websites_are_normalized_without_invention() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        public = Company(
            source="yandex_maps", company_name="Пример", normalized_name="пример",
            website="https://www.example.ru/", website_domain="www.example.ru", raw_data={},
        )
        maps = Company(
            source="yandex_maps", company_name="Карточка", normalized_name="карточка",
            website="https://yandex.ru/maps/org/1", website_domain="yandex.ru", raw_data={},
        )
        session.add_all([public, maps])
        session.commit()

        assert backfill_structured_business_fields(session) == 2
        assert public.website == "https://example.ru"
        assert public.website_domain == "example.ru"
        assert maps.website is None
        assert maps.website_domain is None
