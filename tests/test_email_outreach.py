from email.message import EmailMessage
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings, validate_production_secrets
from app.db import Base
from app.models import Campaign, Company, CompanyDirection, Direction, EmailDelivery, EmailEvent, EmailTemplate, InboundReply, MailAccount, Suppression, TrackedLink
from app.schemas import MailAccountRead
from app.services.imap_monitor import classify_bounce, process_inbound
from app.services.mailing import build_delivery, build_email_message, claim_delivery, diagnose_smtp, queue_campaign, send_claimed_delivery, send_delivery
from app.services.secrets import encrypt_secret


class FakeSMTP:
    def __enter__(self): return self
    def __exit__(self, *_): return None
    def login(self, *_): return (235, b"ok")
    def send_message(self, message): self.message = message; return {}


def setup(session: Session):
    direction = Direction(name="Кафе", slug="кафе", sheet_tab="Кафе", limit_new=10)
    company = Company(source="two_gis", company_name="Кафе Тест", city="Москва", email="hello@example.test", company_email="hello@example.test", normalized_name="кафе тест", raw_data={})
    account = MailAccount(name="Тест", from_email="sender@example.test", from_name="Мария", smtp_host="smtp.example.test", smtp_port=465, smtp_login="sender", smtp_password_encrypted=encrypt_secret("secret"), smtp_security="ssl", imap_host="imap.example.test", imap_port=993, imap_login="sender", imap_password_encrypted=encrypt_secret("secret"), imap_security="ssl", daily_limit=5)
    session.add_all([direction, company, account]); session.flush()
    template = EmailTemplate(name="Кафе", direction_id=direction.id, subject_template="Для {{company_name}}", html_template='<p>Здравствуйте, {{company_name}}</p><a href="https://example.org/a">Подробнее</a>', text_template="Здравствуйте, {{company_name}}")
    session.add(template); session.flush()
    session.add(CompanyDirection(company_id=company.id, direction_id=direction.id)); session.commit()
    return direction, company, account, template


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def test_mailbox_passwords_are_write_only(db: Session) -> None:
    _, _, account, _ = setup(db)
    payload = MailAccountRead.model_validate(account).model_dump()
    assert "smtp_password" not in payload and "imap_password" not in payload
    assert "smtp_password_encrypted" not in payload and "imap_password_encrypted" not in payload


def test_proposal_logo_is_attached_inline(db: Session) -> None:
    _, company, account, template = setup(db)
    delivery = build_delivery(db, company, account, template, Settings(public_base_url="https://leadflow.example"))
    delivery.html_body = '<html><img alt="Богородский пряник" src="https://leadflow.example/email-assets/bogorodsky-pryanik-logo.jpg"></html>'
    delivery.message_id = "<test@example.test>"
    message = build_email_message(delivery, account)
    html_part = next(part for part in message.walk() if part.get_content_type() == "text/html")
    logo_part = next(part for part in message.walk() if part.get("Content-ID") == "<bogorodsky-pryanik-logo>")
    assert "cid:bogorodsky-pryanik-logo" in html_part.get_content()
    assert logo_part.get_content_type() == "image/jpeg"
    assert len(logo_part.get_payload(decode=True)) > 10_000


def test_campaign_queue_is_direction_limited_and_idempotent(db: Session) -> None:
    direction, _, account, template = setup(db)
    campaign = Campaign(name="Кафе", direction_id=direction.id, mailbox_id=account.id, template_id=template.id, daily_limit=5, run_limit=5, sending_interval_seconds=10, cooldown_days=30, status="running", active=True)
    db.add(campaign); db.commit()
    settings = Settings(
        public_base_url="https://lead.example.com",
        email_open_tracking_enabled=True,
        email_click_tracking_enabled=True,
    )
    first = queue_campaign(db, campaign, settings)
    second = queue_campaign(db, campaign, settings)
    assert first["queued"] == 1 and second["queued"] == 0
    delivery = db.scalar(select(EmailDelivery))
    assert delivery and delivery.direction_id == direction.id and delivery.status == "queued"
    assert len(delivery.tracking_token) >= 32 and len(delivery.unsubscribe_token) >= 32
    assert "/unsubscribe/" in delivery.html_body and "/t/open/" in delivery.html_body
    assert db.scalar(select(TrackedLink)) is not None


def test_campaign_daily_limit_is_separate_from_mailbox_daily_usage(db: Session) -> None:
    direction, company, account, template = setup(db)
    manual = build_delivery(db, company, account, template, Settings(public_base_url="https://lead.example.com"))
    manual.status = "sent"
    manual.sent_at = datetime.now(timezone.utc)
    campaign = Campaign(
        name="Первое письмо кампании", direction_id=direction.id, mailbox_id=account.id,
        template_id=template.id, daily_limit=1, run_limit=1, cooldown_days=0,
        status="running", active=True,
    )
    db.add(campaign); db.commit()

    result = queue_campaign(db, campaign, Settings(public_base_url="https://lead.example.com"))

    assert result["queued"] == 1
    assert db.scalar(select(EmailDelivery).where(EmailDelivery.campaign_id == campaign.id)) is not None


def test_deliverability_defaults_keep_direct_links_and_add_unsubscribe_headers(db: Session) -> None:
    _, company, account, template = setup(db)
    delivery = build_delivery(db, company, account, template, Settings(public_base_url="https://lead.example.com"))
    delivery.message_id = "<test@example.test>"
    message = build_email_message(delivery, account)

    assert 'href="https://example.org/a"' in delivery.html_body
    assert "/t/open/" not in delivery.html_body
    assert db.scalar(select(TrackedLink)) is None
    assert delivery.unsubscribe_url in delivery.html_body
    assert delivery.unsubscribe_url in delivery.text_body
    assert message["Date"]
    assert message["List-Unsubscribe"] == f"<{delivery.unsubscribe_url}>"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_ip_origin_disables_tracking_and_uses_mailto_unsubscribe(db: Session) -> None:
    _, company, account, template = setup(db)
    company.email = company.company_email = "client@example.com"
    db.commit()
    delivery = build_delivery(db, company, account, template, Settings(
        public_base_url="https://159.194.253.61",
        email_open_tracking_enabled=True,
        email_click_tracking_enabled=True,
    ))
    delivery.message_id = "<test@example.test>"
    message = build_email_message(delivery, account)
    assert 'href="https://example.org/a"' in delivery.html_body
    assert "159.194.253.61" not in delivery.html_body
    assert "/t/open/" not in delivery.html_body
    assert delivery.unsubscribe_url.startswith("mailto:")
    assert message["List-Unsubscribe"] == f"<{delivery.unsubscribe_url}>"
    assert message["List-Unsubscribe-Post"] is None


def test_suppression_checked_before_queue(db: Session) -> None:
    _, company, account, template = setup(db)
    db.add(Suppression(email="hello@example.test", reason="manual", active=True)); db.commit()
    with pytest.raises(ValueError, match="suppressed"):
        build_delivery(db, company, account, template, Settings())


def test_daily_mailbox_limit_applies_to_one_off_sends(db: Session) -> None:
    _, company, account, template = setup(db)
    account.daily_limit = 1
    build_delivery(db, company, account, template, Settings(), recipient_override="first@example.test")
    with pytest.raises(ValueError, match="daily limit"):
        build_delivery(db, company, account, template, Settings(), recipient_override="second@example.test")


def test_atomic_claim_and_send_updates_company(db: Session, monkeypatch) -> None:
    _, company, account, template = setup(db)
    delivery = build_delivery(db, company, account, template, Settings(public_base_url="https://lead.test"))
    monkeypatch.setattr("app.services.mailing.smtp_connection", lambda _: FakeSMTP())
    claimed = claim_delivery(db, "worker-a")
    assert claimed == delivery.id
    assert claim_delivery(db, "worker-b") is None
    assert send_claimed_delivery(db, delivery.id, "worker-a") == "sent"
    db.refresh(company); db.refresh(delivery)
    assert delivery.message_id and delivery.sent_at and delivery.attempt_count == 1
    assert company.action == "Отправлено предложение" and company.communication_started_at
    assert [row.event_type for row in db.scalars(select(EmailEvent).order_by(EmailEvent.occurred_at))] == ["queued", "sending", "sent"]


def test_unavailable_mailbox_records_error_and_defers_retry(db: Session) -> None:
    _, company, account, template = setup(db)
    delivery = build_delivery(db, company, account, template, Settings(public_base_url="https://lead.test"))
    account.active = False; db.commit()
    send_delivery(db, delivery, account)
    db.refresh(delivery)
    assert delivery.status == "send_error" and delivery.next_attempt_at is not None
    account.active = True; db.commit()
    assert claim_delivery(db, "too-early-retry") is None
    assert [row.event_type for row in db.scalars(select(EmailEvent).order_by(EmailEvent.occurred_at))] == [
        "queued", "sending", "send_error",
    ]


def test_automatic_campaign_uses_matching_direction_template(db: Session) -> None:
    direction, company, account, matching = setup(db)
    wrong = EmailTemplate(
        name="Отели", direction_id=None, subject_template="Неверный шаблон",
        html_template="<p>Неверный шаблон</p>", text_template="Неверный шаблон",
    )
    db.add(wrong); db.flush()
    campaign = Campaign(
        name="Автоматическая рассылка", direction_id=direction.id, mailbox_id=account.id,
        template_id=wrong.id, daily_limit=1, run_limit=1, sending_interval_seconds=10,
        cooldown_days=0, status="running", active=True,
    )
    db.add(campaign); db.commit()
    assert queue_campaign(db, campaign, Settings(public_base_url="https://lead.test"))["queued"] == 1
    delivery = db.scalar(select(EmailDelivery).where(EmailDelivery.company_id == company.id))
    assert delivery.template_id == matching.id
    assert company.company_name in delivery.subject
    assert company.company_name in delivery.html_body


def test_reply_matching_and_uid_idempotency(db: Session) -> None:
    _, company, account, template = setup(db)
    delivery = build_delivery(db, company, account, template, Settings())
    delivery.status, delivery.message_id = "sent", "<original@example.test>"; db.commit()
    reply = EmailMessage(); reply["From"] = delivery.recipient_email; reply["To"] = account.from_email
    reply["Message-ID"] = "<reply@example.test>"; reply["In-Reply-To"] = delivery.message_id; reply.set_content("Да, интересно")
    assert process_inbound(db, account, reply.as_bytes(), 42, 7) is True
    assert process_inbound(db, account, reply.as_bytes(), 42, 7) is False
    db.refresh(delivery); db.refresh(company)
    assert delivery.status == "replied" and db.query(InboundReply).count() == 1
    assert db.scalar(select(EmailEvent.event_type).where(EmailEvent.delivery_id == delivery.id, EmailEvent.event_type == "replied")) == "replied"
    assert company.action == "Получен ответ"


def test_hard_bounce_classification_and_suppression(db: Session) -> None:
    _, _, account, template = setup(db)
    company = db.scalar(select(Company)); delivery = build_delivery(db, company, account, template, Settings())
    delivery.status, delivery.message_id = "sent", "<original@example.test>"; db.commit()
    dsn = EmailMessage(); dsn["From"] = "mailer-daemon@example.test"; dsn["To"] = account.from_email
    dsn["Message-ID"] = "<dsn@example.test>"; dsn["References"] = delivery.message_id; dsn["Subject"] = "Delivery Status Notification"; dsn.set_content("Status: 5.1.1 user unknown")
    assert classify_bounce(dsn, "mailer-daemon@example.test", dsn["Subject"], "Status: 5.1.1") == "hard_bounce"
    process_inbound(db, account, dsn.as_bytes(), 43, 7)
    db.refresh(delivery)
    assert delivery.status == "bounced" and db.get(Suppression, delivery.recipient_email).reason == "hard_bounce"
    assert db.scalar(select(EmailEvent.event_type).where(EmailEvent.delivery_id == delivery.id, EmailEvent.event_type == "bounced")) == "bounced"


def test_smtp_diagnostics_separate_connection_and_auth(db: Session, monkeypatch) -> None:
    _, _, account, _ = setup(db)
    monkeypatch.setattr("app.services.mailing.smtp_connection", lambda _: FakeSMTP())
    assert diagnose_smtp(account) == {"connection": True, "authentication": True}


def test_production_rejects_default_secrets() -> None:
    with pytest.raises(RuntimeError):
        validate_production_secrets(Settings(app_environment="production", admin_password="change-me-before-production"))
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        validate_production_secrets(Settings(app_environment="production", admin_password="unique-admin-password", secret_key="x" * 40))
    validate_production_secrets(Settings(
        app_environment="production", admin_password="unique-admin-password", secret_key="x" * 40,
        public_base_url="https://leadflow.example.com",
    ))
