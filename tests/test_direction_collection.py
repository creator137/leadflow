from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Company, CompanyDirection, CompanySourceRecord, DirectionRun
from app.schemas import DirectionCreate, DirectionLocationInput, DirectionQueryInput
from app.services.direction_collection import execute_direction
from app.services.directions import create_direction
from app.sources.base import CompanyLead, SearchSpec, SourceAdapter, SourceBlocked


class DirectionAdapter(SourceAdapter):
    def __init__(self, source: str):
        self.name = source

    def collect(self, spec: SearchSpec):
        for number in range(1, 5):
            yield CompanyLead(
                source=self.name,
                source_external_id=f"{self.name}-{spec.query}-{number}",
                source_url=f"https://maps.test/{self.name}/{spec.query}/{number}",
                company_name=f"Компания {number}",
                city=spec.city,
                address=f"ул. Тестовая, {number}",
                phone=f"+7 999 000 00 0{number}",
            )


def make_direction(session: Session, limit: int = 2):
    return create_direction(session, DirectionCreate(
        name="Event-агентства", sheet_tab="Event-агентства", limit_new=limit,
        queries=[DirectionQueryInput(query="event агентство"), DirectionQueryInput(query="организация мероприятий")],
        locations=[DirectionLocationInput(city="Москва")],
        sources=["yandex_maps", "two_gis"],
    ))


def test_direction_limit_is_shared_and_repeat_finds_next(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = make_direction(session)
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda source, _: DirectionAdapter(source))
        settings = Settings(database_url="sqlite+pysqlite:///:memory:", source_max_scan=20)

        first = execute_direction(session, direction, settings)
        assert (first.status, first.inserted) == ("completed", 2)
        assert session.query(CompanyDirection).count() == 2

        second = execute_direction(session, direction, settings)
        assert second.status == "completed"
        assert second.inserted == 2
        assert second.duplicates >= 2
        assert session.query(CompanyDirection).count() == 4


def test_cross_source_company_is_canonical_and_provenance_retained(monkeypatch) -> None:
    class SameCompanyAdapter(SourceAdapter):
        def __init__(self, source): self.name = source
        def collect(self, spec):
            yield CompanyLead(
                source=self.name, source_external_id=f"{self.name}-abc", source_url=f"https://{self.name}/abc",
                company_name="Ресторан ABC", city="Москва", address="Ленина, 1", phone="+7 999 111-22-33",
            )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = make_direction(session, limit=2)
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda source, _: SameCompanyAdapter(source))
        run = execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"))
        assert run.status == "exhausted"
        assert session.query(Company).count() == 1
        assert session.query(CompanyDirection).count() == 1
        assert session.query(CompanySourceRecord).count() == 2


def test_direction_resume_uses_checkpoint(monkeypatch) -> None:
    class Interrupted(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec):
            yield CompanyLead(source=self.name, source_external_id="resume-a", source_url="https://maps/a",
                              company_name="A", phone="+7 999 800-00-01")
            raise RuntimeError("connection interrupted")

    class Resumed(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec):
            yield CompanyLead(source=self.name, source_external_id="resume-a", source_url="https://maps/a",
                              company_name="A", phone="+7 999 800-00-01")
            yield CompanyLead(source=self.name, source_external_id="resume-b", source_url="https://maps/b",
                              company_name="B", phone="+7 999 800-00-02")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Resume", sheet_tab="Resume", limit_new=2,
            queries=[DirectionQueryInput(query="test")],
            locations=[DirectionLocationInput(city="Москва")], sources=["yandex_maps"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: Interrupted())
        run = execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"))
        assert run.status == "failed" and run.inserted == 1
        assert run.checkpoint["last_external_id"] == "resume-a"
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: Resumed())
        run = execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"), run)
        assert run.status == "completed" and run.inserted == 2
        assert session.query(CompanyDirection).count() == 2


def test_direction_terminal_statuses(monkeypatch) -> None:
    class Empty(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec): return iter(())
    class Blocked(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec): raise SourceBlocked("captcha")
    class Failed(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec): raise RuntimeError("network")

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Statuses", sheet_tab="Statuses", limit_new=1,
            queries=[DirectionQueryInput(query="test")],
            locations=[DirectionLocationInput(city="Москва")], sources=["yandex_maps"],
        ))
        settings = Settings(database_url="sqlite+pysqlite:///:memory:")
        for adapter, expected in ((Empty(), "exhausted"), (Blocked(), "blocked"), (Failed(), "failed")):
            monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_, value=adapter: value)
            assert execute_direction(session, direction, settings).status == expected
