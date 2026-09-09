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
