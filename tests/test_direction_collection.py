from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Company, CompanyDirection, CompanyFieldProvenance, CompanySourceRecord, Direction, DirectionLocation, DirectionRun, SearchObservation
from app.schemas import DirectionCreate, DirectionLocationInput, DirectionQueryInput
from app.services.direction_collection import execute_direction, expand_locations
from app.services.region_catalog import available_regions, cities_for_region
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


def test_moscow_oblast_location_expands_to_concrete_cities() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Область", sheet_tab="Область", limit_new=50,
            queries=[DirectionQueryInput(query="ресторан")],
            locations=[DirectionLocationInput(city="Московская область", region="Московская область", scope="region")],
            sources=["yandex_maps"],
        ))
        location = session.scalar(select(DirectionLocation).where(DirectionLocation.direction_id == direction.id))

        expanded = expand_locations([location])

        assert len(expanded) == 74
        assert {item.city for item in expanded} >= {"Химки", "Подольск", "Мытищи"}
        assert all(item.region == "Московская область" for item in expanded)
        assert len({item.key for item in expanded}) == 74


def test_explicit_city_is_searched_before_regional_cities(monkeypatch) -> None:
    visited = []

    class EmptyAdapter(SourceAdapter):
        name = "yandex_maps"

        def collect(self, spec):
            visited.append(spec.city)
            return iter(())

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Город и область", sheet_tab="Город и область", limit_new=1,
            queries=[DirectionQueryInput(query="кейтеринг")],
            locations=[
                DirectionLocationInput(city="Ленинградская область", region="Ленинградская область", scope="region"),
                DirectionLocationInput(city="Санкт-Петербург"),
            ],
            sources=["yandex_maps"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: EmptyAdapter())

        execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"))

        assert visited[0] == "Санкт-Петербург"


def test_catalog_covers_all_supported_russian_subjects() -> None:
    regions = available_regions()

    assert len(regions) == 85
    assert len(cities_for_region("Свердловская область")) == 47
    assert len(cities_for_region("Краснодарский край")) == 26
    assert not cities_for_region("Несуществующая область")


def test_regional_run_uses_actual_city_and_shared_limit(monkeypatch) -> None:
    seen_cities = []

    class RegionAdapter(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec):
            seen_cities.append(spec.city)
            yield CompanyLead(
                source=self.name, source_external_id=f"region-{spec.city}",
                source_url=f"https://maps.test/{spec.city}", company_name=f"Ресторан {spec.city}",
                city=spec.city, address=f"{spec.city}, 1", phone=f"+7 999 100 00 {len(seen_cities):02d}",
            )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Региональный", sheet_tab="Региональный", limit_new=3,
            queries=[DirectionQueryInput(query="ресторан")],
            locations=[DirectionLocationInput(city="Московская область", region="Московская область", scope="region")],
            sources=["yandex_maps"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: RegionAdapter())

        run = execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"))

        assert run.inserted == 3
        assert len(seen_cities) == 3
        assert {company.city for company in session.scalars(select(Company))} == set(seen_cities)


def test_regional_run_uses_queried_city_when_source_card_omits_city(monkeypatch) -> None:
    class NoCityAdapter(SourceAdapter):
        name = "yandex_maps"
        def collect(self, spec):
            yield CompanyLead(
                source=self.name, source_external_id="missing-city", source_url="https://maps.test/no-city",
                company_name="Ресторан без города", address="Тестовый адрес", phone="+7 999 111-22-33",
            )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Региональный без города", sheet_tab="Региональный без города", limit_new=1,
            queries=[DirectionQueryInput(query="ресторан")],
            locations=[DirectionLocationInput(city="Московская область", region="Московская область", scope="region")],
            sources=["yandex_maps"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: NoCityAdapter())

        execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"))

        assert session.scalar(select(Company.city)) == "Апрелевка"


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


def test_existing_company_in_global_database_is_added_to_new_direction(monkeypatch) -> None:
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

        assert run.inserted == 1
        assert run.duplicates == 0
        assert session.query(Company).count() == 1
        assert session.query(CompanyDirection).filter_by(direction_id=direction.id).count() == 1
        assert session.scalar(select(SearchObservation).where(SearchObservation.direction_id == direction.id)).is_new is True


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


def test_yandex_cursor_resumes_at_page_and_position(monkeypatch) -> None:
    seen_starts = []
    seen_limits = []

    class PagedAdapter(SourceAdapter):
        name = "yandex_maps"

        def collect(self, spec):
            start = (spec.options["start_page"], spec.options["start_index"])
            seen_starts.append(start)
            seen_limits.append(spec.limit)
            for page in range(start[0], 4):
                for index in range(start[1] if page == start[0] else 0, 3):
                    number = (page - 1) * 3 + index
                    yield CompanyLead(
                        source=self.name, source_external_id=f"page-{page}-card-{index}",
                        source_url=f"https://maps.test/{page}/{index}",
                        company_name=f"Компания {number}", phone=f"+7 999 700-01-{number:02d}",
                        source_page=page, source_index=index, source_page_size=3,
                    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Постраничный поиск", sheet_tab="Постраничный поиск", limit_new=2,
            queries=[DirectionQueryInput(query="кейтеринг")],
            locations=[DirectionLocationInput(city="Москва")], sources=["yandex_maps"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: PagedAdapter())
        settings = Settings(database_url="sqlite+pysqlite:///:memory:")

        for _ in range(3):
            assert execute_direction(session, direction, settings).inserted == 2

        assert seen_starts == [(1, 0), (1, 2), (2, 1)]
        assert seen_limits == [150, 150, 150]
        assert session.query(Company).count() == 6
        assert next(iter(direction.search_cursor.values())) == {"version": 2, "page": 3, "index": 0}


def test_exhausted_yandex_search_probes_head_without_reprocessing_old_card(monkeypatch) -> None:
    seen_options = []

    class FinishedAdapter(SourceAdapter):
        name = "yandex_maps"

        def collect(self, spec):
            seen_options.append(dict(spec.options))
            yield CompanyLead(
                source=self.name, source_external_id="known-card", source_url="https://maps.test/known",
                company_name="Компания", phone="+7 999 700-02-01",
                source_page=1, source_index=0, source_page_size=1,
            )
            spec.options["_source_exhausted"] = True

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Завершённый поиск", sheet_tab="Завершённый поиск", limit_new=2,
            queries=[DirectionQueryInput(query="кейтеринг")],
            locations=[DirectionLocationInput(city="Москва")], sources=["yandex_maps"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: FinishedAdapter())
        settings = Settings(database_url="sqlite+pysqlite:///:memory:")

        first = execute_direction(session, direction, settings)
        second = execute_direction(session, direction, settings)

        assert first.inserted == 1 and second.inserted == 0
        assert second.scanned == 0 and second.duplicates == 0
        assert seen_options[0]["max_pages"] == 20 and seen_options[1]["max_pages"] == 1
        assert next(iter(direction.search_cursor.values()))["exhausted_at"]


def test_unsupported_two_gis_city_is_skipped_without_error(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, DirectionCreate(
            name="Неподдерживаемый город", sheet_tab="Неподдерживаемый город", limit_new=2,
            queries=[DirectionQueryInput(query="кейтеринг")],
            locations=[DirectionLocationInput(city="Выборг")], sources=["two_gis"],
        ))
        monkeypatch.setattr("app.services.direction_collection.build_adapter", lambda *_: (_ for _ in ()).throw(AssertionError("2GIS should not start")))

        run = execute_direction(session, direction, Settings(database_url="sqlite+pysqlite:///:memory:"))

        assert run.inserted == 0 and run.errors == 0
        assert "городов без поддержки в каталоге источника — 1" in run.message


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
