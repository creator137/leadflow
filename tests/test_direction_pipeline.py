from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Company, CompanyDirection, Direction, DirectionRun
from app.services.direction_pipeline import execute_direction_pipeline


def test_one_broken_website_does_not_stop_direction_enrichment(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    started = datetime.now(timezone.utc)
    calls: list[str] = []

    class FakeEnrichment:
        def __init__(self, *_args, **_kwargs):
            pass

        def enrich(self, company, **_kwargs):
            calls.append(company.company_name)
            if company.company_name == "Сломанный сайт":
                raise AttributeError("bad html")

    with Session(engine) as session:
        direction = Direction(
            name="Тест", slug="test", sheet_tab="Тест",
            email_enrichment_enabled=True, ai_enrichment_enabled=True,
        )
        broken = Company(
            source="yandex_maps", company_name="Сломанный сайт", normalized_name="сломанный сайт",
            website="https://broken.test", raw_data={},
        )
        working = Company(
            source="yandex_maps", company_name="Рабочий сайт", normalized_name="рабочий сайт",
            website="https://working.test", raw_data={},
        )
        session.add_all([direction, broken, working])
        session.flush()
        run = DirectionRun(
            direction_id=direction.id, limit_new=2, inserted=2, status="completed",
            started_at=started, finished_at=started + timedelta(minutes=1),
        )
        session.add(run)
        session.add_all([
            CompanyDirection(company_id=broken.id, direction_id=direction.id, created_at=started),
            CompanyDirection(company_id=working.id, direction_id=direction.id, created_at=started),
        ])
        session.commit()

        monkeypatch.setattr("app.services.direction_pipeline.execute_direction", lambda *_args: run)
        monkeypatch.setattr("app.services.direction_pipeline.CompanyEnrichmentService", FakeEnrichment)
        monkeypatch.setattr("app.services.direction_pipeline.active_sheets_config", lambda *_args: None)

        execute_direction_pipeline(session, direction, Settings(_env_file=None))

    assert calls == ["Сломанный сайт", "Рабочий сайт"]
