from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Direction, DirectionLocation, DirectionSearchQuery, DirectionSource
from app.schemas import DirectionCreate, DirectionRead, DirectionUpdate


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zа-яё0-9]+", "-", value.casefold(), flags=re.IGNORECASE).strip("-")
    return slug or "direction"


def serialize_direction(session: Session, direction: Direction) -> DirectionRead:
    queries = list(session.scalars(
        select(DirectionSearchQuery).where(DirectionSearchQuery.direction_id == direction.id)
        .order_by(DirectionSearchQuery.priority, DirectionSearchQuery.created_at)
    ))
    locations = list(session.scalars(
        select(DirectionLocation).where(DirectionLocation.direction_id == direction.id).order_by(DirectionLocation.city)
    ))
    sources = list(session.scalars(
        select(DirectionSource.source).where(DirectionSource.direction_id == direction.id, DirectionSource.active.is_(True))
        .order_by(DirectionSource.source)
    ))
    return DirectionRead.model_validate({
        **{column.name: getattr(direction, column.name) for column in Direction.__table__.columns},
        "queries": queries,
        "locations": locations,
        "sources": sources,
    })


def _unique_slug(session: Session, name: str, direction_id: str | None = None) -> str:
    base = slugify(name)
    candidate = base
    suffix = 2
    while session.scalar(select(Direction.id).where(Direction.slug == candidate, Direction.id != direction_id)):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def replace_children(session: Session, direction: Direction, payload: DirectionCreate | DirectionUpdate) -> None:
    if payload.queries is not None:
        session.execute(delete(DirectionSearchQuery).where(DirectionSearchQuery.direction_id == direction.id))
        session.add_all(DirectionSearchQuery(direction_id=direction.id, **item.model_dump()) for item in payload.queries)
    if payload.locations is not None:
        session.execute(delete(DirectionLocation).where(DirectionLocation.direction_id == direction.id))
        session.add_all(DirectionLocation(direction_id=direction.id, **item.model_dump()) for item in payload.locations)
    if payload.sources is not None:
        session.execute(delete(DirectionSource).where(DirectionSource.direction_id == direction.id))
        session.add_all(DirectionSource(direction_id=direction.id, source=source) for source in dict.fromkeys(payload.sources))


def create_direction(session: Session, payload: DirectionCreate) -> Direction:
    values = payload.model_dump(exclude={"queries", "locations", "sources"})
    direction = Direction(**values, slug=_unique_slug(session, payload.name))
    session.add(direction)
    session.flush()
    replace_children(session, direction, payload)
    session.commit()
    session.refresh(direction)
    return direction


def update_direction(session: Session, direction: Direction, payload: DirectionUpdate) -> Direction:
    values = payload.model_dump(exclude_unset=True, exclude={"queries", "locations", "sources"})
    for key, value in values.items():
        setattr(direction, key, value)
    if payload.name is not None:
        direction.slug = _unique_slug(session, payload.name, direction.id)
    replace_children(session, direction, payload)
    session.commit()
    session.refresh(direction)
    return direction


def archive_direction(session: Session, direction: Direction) -> None:
    direction.active = False
    direction.archived_at = datetime.now(timezone.utc)
    session.commit()
