import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import DirectionLocation, DirectionSearchQuery, DirectionSource
from app.schemas import DirectionCreate, DirectionLocationInput, DirectionQueryInput, DirectionUpdate
from app.services.directions import archive_direction, create_direction, serialize_direction, update_direction


def payload(name: str = "Рестораны") -> DirectionCreate:
    return DirectionCreate(
        name=name,
        sheet_tab=name,
        limit_new=10,
        queries=[DirectionQueryInput(query="ресторан"), DirectionQueryInput(query="рестораны", priority=200)],
        locations=[DirectionLocationInput(city="Москва", region="Москва")],
        sources=["yandex_maps", "two_gis"],
    )


def test_direction_crud_and_normalized_children() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = create_direction(session, payload())
        read = serialize_direction(session, direction)
        assert read.name == "Рестораны"
        assert len(read.queries) == 2
        assert len(read.locations) == 1
        assert set(read.sources) == {"yandex_maps", "two_gis"}
        assert session.query(DirectionSearchQuery).count() == 2
        assert session.query(DirectionLocation).count() == 1
        assert session.query(DirectionSource).count() == 2

        update_direction(session, direction, DirectionUpdate(
            name="Музеи", sheet_tab="Музеи", limit_new=20,
            queries=[DirectionQueryInput(query="музей")],
            locations=[DirectionLocationInput(city="Москва")],
            sources=["yandex_maps"],
        ))
        read = serialize_direction(session, direction)
        assert (read.name, read.slug, read.sheet_tab, read.limit_new) == ("Музеи", "музеи", "Музеи", 20)
        assert [item.query for item in read.queries] == ["музей"]
        assert read.sources == ["yandex_maps"]

        archive_direction(session, direction)
        assert direction.active is False and direction.archived_at is not None


def test_new_direction_defaults_to_fifty_companies_daily() -> None:
    data = DirectionCreate(
        name="Отели", sheet_tab="Отели",
        queries=[DirectionQueryInput(query="отель")],
        locations=[DirectionLocationInput(city="Москва")],
        sources=["yandex_maps"],
    )
    assert data.limit_new == 50
    assert data.schedule == "0 5 * * *"


def test_active_direction_cannot_accidentally_lose_daily_schedule() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        data = DirectionCreate(
            name="Школы", sheet_tab="Школы", active=True, schedule=None,
            queries=[DirectionQueryInput(query="школа")],
            locations=[DirectionLocationInput(city="Москва")], sources=["two_gis"],
        )
        direction = create_direction(session, data)
        assert direction.schedule == "0 5 * * *"
        direction.schedule = None
        session.commit()
        update_direction(session, direction, DirectionUpdate(active=True))
        assert direction.schedule == "0 5 * * *"


def test_region_location_requires_supported_catalog_region() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        payload = DirectionCreate(
            name="Тест области", sheet_tab="Тест области", active=True,
            queries=[DirectionQueryInput(query="ресторан")],
            locations=[DirectionLocationInput(city="Несуществующая область", region="Несуществующая область", scope="region")],
            sources=["yandex_maps"],
        )
        with pytest.raises(ValueError, match="пока не поддерживается"):
            create_direction(session, payload)
