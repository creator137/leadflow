from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    Company, CompanyDirection, Direction, DirectionRun, EmailDelivery, EmailTemplate,
    MailAccount, SearchObservation,
)
from app.services.analytics import analytics


def seed(session: Session):
    now = datetime.now(timezone.utc)
    direction = Direction(name="Рестораны", slug="restaurants", sheet_tab="Рестораны", active=True, limit_new=10)
    other = Direction(name="Отели", slug="hotels", sheet_tab="Отели", active=True, limit_new=10)
    mailbox = MailAccount(name="Основная", from_email="mail@example.ru", smtp_host="smtp.example.ru", smtp_login="mail@example.ru", smtp_password_encrypted="x", imap_host="imap.example.ru", imap_login="mail@example.ru", imap_password_encrypted="x")
    template = EmailTemplate(name="Первое письмо", subject_template="Здравствуйте", html_template="<p>Текст</p>", text_template="Текст")
    session.add_all([direction, other, mailbox, template]); session.flush()
    company = Company(source="yandex_maps", company_name="Ромашка", city="Москва", address="Ленина, 1", company_email="info@example.ru", company_phone="+79990000000", website="https://example.ru", branches_count=3, normalized_name="ромашка", raw_data={}, created_at=now, collected_at=now)
    duplicate = Company(source="two_gis", company_name="Лилия", city="Казань", normalized_name="лилия", raw_data={}, created_at=now, collected_at=now)
    session.add_all([company, duplicate]); session.flush()
    session.add_all([CompanyDirection(company_id=company.id, direction_id=direction.id), CompanyDirection(company_id=duplicate.id, direction_id=other.id)])
    run = DirectionRun(direction_id=direction.id, limit_new=10, status="completed", scanned=2, inserted=1, duplicates=1, started_at=now, finished_at=now)
    session.add(run); session.flush()
    session.add_all([
        SearchObservation(run_id=run.id, direction_id=direction.id, company_id=company.id, source="yandex_maps", query="ресторан", city="Москва", is_new=True, has_phone=True, has_email=True, has_website=True, has_branches_count=True, observed_at=now),
        SearchObservation(run_id=run.id, direction_id=direction.id, company_id=company.id, source="two_gis", query="кафе", city="Москва", is_new=False, has_phone=True, has_email=False, has_website=True, has_branches_count=True, observed_at=now),
    ])
    session.add(EmailDelivery(company_id=company.id, direction_id=direction.id, mailbox_id=mailbox.id, template_id=template.id, recipient_email="info@example.ru", subject="Здравствуйте", html_body="<p>Текст</p>", text_body="Текст", status="replied", tracking_token="track", unsubscribe_token="unsub", sent_at=now, opened_at=now, replied_at=now, created_at=now))
    session.commit()
    return direction


def test_analytics_aggregations_match_reference_sql_and_filters() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = seed(session)
        result = analytics(session)
        sql_new = session.scalar(select(func.count()).select_from(SearchObservation).where(SearchObservation.is_new.is_(True)))
        assert result["overview"]["new"] == sql_new == 1
        assert result["overview"]["duplicates"] == 1
        assert result["overview"]["with_website"] == 1
        assert result["email"]["sent"] == result["email"]["replied"] == 1
        assert {row["name"] for row in result["sources"]["rows"]} == {"yandex_maps", "two_gis"}
        assert {row["name"] for row in result["queries"]} == {"ресторан", "кафе"}
        assert result["cities"][0]["name"] == "Москва"
        filtered = analytics(session, direction=direction.id, city="Москва", source="yandex_maps")
        assert filtered["overview"]["new"] == 1 and filtered["overview"]["duplicates"] == 0
        assert filtered["data_quality"]["total"] == 1
