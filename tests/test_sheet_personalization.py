from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Company, CompanyDirection, Direction, EmailDelivery, EmailTemplate, GoogleSheetsConfig, MailAccount, SheetPersonalizationDraft
from app.services.google_sheets import GoogleSheetsSyncService
from app.services.sheet_personalization import process_sheet_personalization_triggers, send_sheet_draft
from app.services.secrets import encrypt_secret


class FakeWorksheet:
    def __init__(self):
        self.rows = []

    def get_all_values(self):
        return [row[:] for row in self.rows]

    def update(self, *, range_name, values):
        from gspread.utils import a1_to_rowcol
        row, column = a1_to_rowcol(range_name.split(":", 1)[0])
        while len(self.rows) < row: self.rows.append([])
        if column == 1 and len(values[0]) > 1: self.rows[row - 1] = values[0][:]
        else:
            while len(self.rows[row - 1]) < column: self.rows[row - 1].append("")
            self.rows[row - 1][column - 1] = values[0][0]

    def batch_update(self, updates):
        for update in updates: self.update(range_name=update["range"], values=update["values"])

    def hide_columns(self, *_): pass


def setup_company(session: Session):
    direction = Direction(name="Рестораны", slug="рестораны", sheet_tab="Рестораны", limit_new=10)
    company = Company(source="yandex_maps", company_name="Тестовая компания", city="Москва", address="Адрес",
                      company_email="owner@example.test", normalized_name="тестовая компания", raw_data={})
    session.add_all([direction, company]); session.flush()
    session.add(CompanyDirection(company_id=company.id, direction_id=direction.id))
    config = GoogleSheetsConfig(spreadsheet_id="sheet", worksheet_name="unused", credentials_encrypted="unused")
    session.add(config); session.commit()
    return direction, company, config


def _mailing_setup(session: Session, direction_id: str) -> tuple[EmailTemplate, MailAccount]:
    template = EmailTemplate(
        name="КП", direction_id=direction_id, subject_template="Предложение для {{ company_name }}",
        html_template="<p>{{ personalized_text }}</p>", text_template="{{ personalized_text }}", active=True,
    )
    mailbox = MailAccount(
        name="Тест", from_email="sender@example.test", smtp_host="smtp.example.test", smtp_port=465,
        smtp_login="sender@example.test", smtp_password_encrypted=encrypt_secret("unused"), smtp_security="ssl",
        imap_host="imap.example.test", imap_port=993, imap_login="sender@example.test",
        imap_password_encrypted=encrypt_secret("unused"), imap_security="ssl", active=True,
    )
    session.add_all([template, mailbox]); session.commit()
    return template, mailbox


def test_sheet_command_prepares_once_by_stable_leadflow_id(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        _mailing_setup(session, direction.id)
        worksheet = FakeWorksheet()
        GoogleSheetsSyncService(session, config).sync_direction(direction, worksheet=worksheet)
        action_column = worksheet.rows[0].index("Действие")
        worksheet.rows[1][action_column] = "Подготовить персональное КП"
        calls = 0

        def fake_prepare(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {
                "request_key": "request-key", "subject": "Персональное КП", "html_body": "<p>Факт</p>",
                "text_body": "Факт", "facts": [{"text": "Проверенный факт", "url": "https://example.test"}],
            }

        monkeypatch.setattr("app.services.sheet_personalization.prepare_personalization", fake_prepare)
        settings = Settings(public_base_url="https://leadflow.example")
        first = process_sheet_personalization_triggers(session, config, direction, settings, worksheet=worksheet)
        second = process_sheet_personalization_triggers(session, config, direction, settings, worksheet=worksheet)

        assert first["prepared"] == 1 and second["reused"] == 1 and calls == 1
        draft = session.scalar(select(SheetPersonalizationDraft))
        assert draft and draft.company_id == company.id and draft.status == "ready"
        status_column = worksheet.rows[0].index("Статус персонального КП")
        preview_column = worksheet.rows[0].index("Предпросмотр КП")
        assert worksheet.rows[1][status_column] == "Готово к отправке"
        assert f"company={company.id}" in worksheet.rows[1][preview_column]
        assert len(worksheet.rows) == 2


def test_sheet_draft_confirmation_is_idempotent(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, config = setup_company(session)
        template, mailbox = _mailing_setup(session, direction.id)
        draft = SheetPersonalizationDraft(
            company_id=company.id, direction_id=direction.id, config_id=config.id, template_id=template.id,
            mailbox_id=mailbox.id, command_key="command", request_key="request", status="ready",
            subject="Тема", html_body="<p>Текст</p>", text_body="Текст", facts=[],
        )
        session.add(draft); session.commit()
        calls = 0

        def fake_send(_session, _company, _mailbox, _template, request_key, _settings, *, overrides=None):
            nonlocal calls
            calls += 1
            existing = _session.scalar(select(EmailDelivery).where(EmailDelivery.idempotency_key == f"personalized:{_company.id}:{request_key}"))
            if existing:
                return existing
            delivery = EmailDelivery(
                company_id=_company.id, mailbox_id=_mailbox.id, template_id=_template.id,
                recipient_email=_company.company_email, subject=overrides["subject"], html_body=overrides["html_body"],
                text_body=overrides["text_body"], status="queued", send_mode="personalized",
                tracking_token="track", unsubscribe_token="unsubscribe",
                idempotency_key=f"personalized:{_company.id}:{request_key}",
            )
            _session.add(delivery); _session.commit(); return delivery

        monkeypatch.setattr("app.services.sheet_personalization.send_personalized", fake_send)
        first = send_sheet_draft(session, draft.id, Settings())
        second = send_sheet_draft(session, draft.id, Settings())
        assert first.id == second.id and calls == 1
        assert session.query(EmailDelivery).count() == 1
