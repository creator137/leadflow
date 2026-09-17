"""Daily direction search defaults and persistent parser cursor."""

import uuid

from alembic import op
import sqlalchemy as sa


revision = "0018_daily_direction_search"
down_revision = "0017_direction_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "directions",
        sa.Column("search_cursor", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )
    op.execute("""
        UPDATE directions
        SET limit_new = 50,
            schedule = COALESCE(schedule, CASE name
                WHEN 'Рестораны' THEN '0 5 * * *'
                WHEN 'Кафе' THEN '10 5 * * *'
                WHEN 'Отели' THEN '20 5 * * *'
                WHEN 'Event-агентства' THEN '30 5 * * *'
                WHEN 'Кейтеринг' THEN '40 5 * * *'
                WHEN 'Туроператоры' THEN '50 5 * * *'
                WHEN 'Школы' THEN '0 6 * * *'
                ELSE '0 5 * * *'
            END)
        WHERE active IS TRUE AND archived_at IS NULL
    """)
    query_defaults = {
        "Кафе": ["кафе", "кофейня", "кондитерская"],
        "Отели": ["отель", "гостиница", "апарт-отель"],
        "Кейтеринг": ["кейтеринг", "выездное обслуживание", "кейтеринговая компания"],
        "Туроператоры": ["туроператор", "туристическая компания", "турагентство"],
        "Школы": ["школа", "частная школа", "образовательный центр"],
    }
    for direction_name, queries in query_defaults.items():
        for priority, query in enumerate(queries, start=1):
            op.execute(sa.text("""
                INSERT INTO direction_search_queries (id, direction_id, query, active, priority, created_at)
                SELECT :id, d.id, :query, true, :priority, now()
                FROM directions d
                WHERE d.name=:direction_name
                  AND NOT EXISTS (
                    SELECT 1 FROM direction_search_queries existing
                    WHERE existing.direction_id=d.id AND existing.query=:query
                  )
            """).bindparams(
                id=str(uuid.uuid4()), query=query, priority=priority * 100,
                direction_name=direction_name,
            ))
        op.execute(sa.text("""
            INSERT INTO direction_locations (id, direction_id, city, region, active)
            SELECT :id, d.id, 'Москва', 'Москва', true
            FROM directions d
            WHERE d.name=:direction_name
              AND NOT EXISTS (SELECT 1 FROM direction_locations existing WHERE existing.direction_id=d.id)
        """).bindparams(id=str(uuid.uuid4()), direction_name=direction_name))
        for source in ("yandex_maps", "two_gis"):
            op.execute(sa.text("""
                INSERT INTO direction_sources (id, direction_id, source, active)
                SELECT :id, d.id, :source, true
                FROM directions d
                WHERE d.name=:direction_name
                  AND NOT EXISTS (
                    SELECT 1 FROM direction_sources existing
                    WHERE existing.direction_id=d.id AND existing.source=:source
                  )
            """).bindparams(id=str(uuid.uuid4()), direction_name=direction_name, source=source))


def downgrade() -> None:
    op.drop_column("directions", "search_cursor")
