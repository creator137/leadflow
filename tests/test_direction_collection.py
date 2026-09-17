from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Company, CompanyDirection, CompanyFieldProvenance, CompanySourceRecord, Direction, DirectionRun, SearchObservation
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
                website=f"https://company-{number}.example",
                branches_count=number if number > 1 else None,
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
        assert direction.search_cursor

        second = execute_direction(session, direction, settings)
        assert second.status == "completed"
        assert second.inserted == 2
        assert second.duplicates >= 2
        assert session.query(CompanyDirection).count() == 4
        assert session.query(SearchObservation).count() >= 4
        assert session.query(CompanyFieldProvenance).filter_by(field="branches_count").count() >= 1


def test_existing_company_in_global_database_is_not_counted_as_new(monkeypatch) -> None:
    class ExistingAdapter(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec):
            yield CompanyLead(
                source=self.name, source_external_id="fresh-source-card", source_url="https://maps.test/existing",
                company_name="Уже в общей базе", city="Москва", address="Ленина, 1",
                phone="+7 999 555-44-33",
            )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        old_direction = Direction(name="Старое", slug="old", sheet_tab="Старое")
        existing = Company(
            source="two_gis", source_external_id="old-card", company_name="Уже в общей базе",
            city="Москва", address="Ленина, 1", phone="+7 999 555-44-33",
            normalized_name="уже в общей базе", normalized_phone="79995554433", raw_data={},
        )
        session.add_all([old_direction, existing]); session.flush()
        session.add(CompanyDirection(company_id=existing.id, direction_id=old_direction.id)); session.commit()
        direction = create_direction(session, DirectionCreate(
            name="Новое", sheet_tab="Новое", limit_new=1,
            queries=[DirectionQueryInput(query="компания")],
            locations=[DirectionLocationInput(city="Москва")], sources=["yandex_maps"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: ExistingAdapter())

        run = execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"))

        assert run.inserted == 0
        assert run.duplicates == 1
        assert session.query(Company).count() == 1
        assert session.query(CompanyDirection).filter_by(direction_id=direction.id).count() == 0
        assert session.scalar(select(SearchObservation).where(SearchObservation.direction_id == direction.id)).is_new is False


def test_daily_cursor_continues_deeper_in_same_result_list(monkeypatch) -> None:
    class LongAdapter(SourceAdapter):
        name = "two_gis"
        def collect(self, spec):
            for number in range(1, 7):
                yield CompanyLead(
                    source=self.name, source_external_id=f"card-{number}",
                    source_url=f"https://2gis.test/{number}", company_name=f"Компания {number}",
                    phone=f"+7 999 700-00-{number:02d}",
                )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Кейтеринг", sheet_tab="Кейтеринг", limit_new=2,
            queries=[DirectionQueryInput(query="кейтеринг")],
            locations=[DirectionLocationInput(city="Москва")], sources=["two_gis"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: LongAdapter())
        settings = Settings(database_url="sqlite+pysqlite:///:memory:", source_max_scan=20)

        first = execute_direction(session, direction, settings)
        second = execute_direction(session, direction, settings)

        assert first.inserted == second.inserted == 2
        assert session.query(Company).count() == 4
        assert next(iter(direction.search_cursor.values()))["offset"] == 4


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
