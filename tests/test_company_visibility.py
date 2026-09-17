from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.main import companies
from app.models import Company, CompanyDirection, Direction


def test_admin_companies_only_include_active_directions() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        active = Direction(name="Активное", slug="active", sheet_tab="Активное", active=True)
        paused = Direction(name="Приостановленное", slug="paused", sheet_tab="Приостановленное", active=False)
        visible = Company(source="manual", company_name="Рабочая", normalized_name="рабочая", raw_data={})
        hidden = Company(source="manual", company_name="Скрытая", normalized_name="скрытая", raw_data={})
        orphan = Company(source="manual", company_name="Без направления", normalized_name="без направления", raw_data={})
        session.add_all([active, paused, visible, hidden, orphan]); session.flush()
        session.add_all([
            CompanyDirection(company_id=visible.id, direction_id=active.id),
            CompanyDirection(company_id=hidden.id, direction_id=paused.id),
        ])
        session.commit()

        rows = companies(
            city=None, category=None, source=None, direction=None, has_website=None,
            limit=100, offset=0, session=session,
        )

        assert [row.id for row in rows] == [visible.id]
