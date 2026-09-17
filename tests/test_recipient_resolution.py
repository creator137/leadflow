from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import (
    Campaign, Company, CompanyDirection, Direction, EmailDelivery, EmailTemplate,
    GoogleSheetsConfig, MailAccount, SheetPersonalizationDraft,
)
from app.services.google_sheets import GoogleSheetsSyncService
from app.services.mailing import build_delivery, queue_campaign
from app.services.proposals import prepare_proposal_draft, update_proposal_draft
from app.services.recipients import resolve_recipient_email
from app.services.secrets import encrypt_secret
from app.services.sheet_personalization import process_sheet_personalization_triggers


class FakeWorksheet:
    def __init__(self): self.rows: list[list[str]] = []
    def get_all_values(self): return [row[:] for row in self.rows]
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


def setup(session: Session):
    direction = Direction(name="Получатели", slug="recipients", sheet_tab="Получатели", active=False)
    company = Company(
        source="manual", source_external_id="recipient-test", company_name="Тест получателя",
        company_email="company@example.test", decision_maker_email="lpr@example.test",
        normalized_name="тест получателя", raw_data={},
    )
    mailbox = MailAccount(
        name="Тест", from_email="sender@example.test", smtp_host="smtp.example.test", smtp_port=465,
        smtp_login="sender", smtp_password_encrypted=encrypt_secret("secret"), smtp_security="ssl",
        imap_host="imap.example.test", imap_port=993, imap_login="sender",
        imap_password_encrypted=encrypt_secret("secret"), imap_security="ssl", active=True, daily_limit=20,
    )
    session.add_all([direction, company, mailbox]); session.flush()
    template = EmailTemplate(
        name="Обычное письмо", direction_id=direction.id, subject_template="Тема",
        html_template="<p>Текст</p>", text_template="Текст", active=True,
    )
    session.add_all([template, CompanyDirection(company_id=company.id, direction_id=direction.id)])
    session.commit()
    return direction, company, mailbox, template


def test_resolver_prioritizes_valid_lpr_and_falls_back_to_company() -> None:
    company = Company(source="manual", company_name="Тест", normalized_name="тест", raw_data={},
                      company_email="Company@Example.Test", decision_maker_email="LPR@Example.Test")
    assert resolve_recipient_email(company) == "lpr@example.test"
    company.decision_maker_email = ""
    assert resolve_recipient_email(company) == "company@example.test"
    company.decision_maker_email = "not-an-email"
    assert resolve_recipient_email(company) == "company@example.test"
    company.company_email = "also-invalid"
    assert resolve_recipient_email(company) is None


def test_both_missing_or_invalid_block_delivery() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:"); Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, company, mailbox, template = setup(session)
        for lpr, general in [(None, None), ("bad", "also-bad")]:
            company.decision_maker_email, company.company_email, company.email = lpr, general, None
            session.commit()
            try: build_delivery(session, company, mailbox, template, Settings())
            except ValueError as exc: assert str(exc) == "У компании не указан email для отправки"
            else: raise AssertionError("Invalid recipients must block sending")


def test_proposal_uses_lpr_and_preserves_manual_override() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:"); Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, company, mailbox, _ = setup(session)
        settings = Settings(public_base_url="https://leadflow.example")
        draft = prepare_proposal_draft(session, company, settings, mailbox=mailbox)
        assert draft.recipient_email == "lpr@example.test"
        update_proposal_draft(session, draft, {"recipient_email": "manual@example.test"}, settings)
        company.decision_maker_email = company.company_email = company.email = None; session.commit()
        same = prepare_proposal_draft(session, company, settings, mailbox=mailbox, regenerate=True)
        assert same.id == draft.id and same.recipient_email == "manual@example.test"


def test_automatic_campaign_uses_same_priority() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:"); Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, _, mailbox, template = setup(session)
        campaign = Campaign(name="Тест", direction_id=direction.id, mailbox_id=mailbox.id,
                            template_id=template.id, daily_limit=5, run_limit=5, cooldown_days=0,
                            status="running", active=True)
        session.add(campaign); session.commit()
        assert queue_campaign(session, campaign, Settings())["queued"] == 1
        assert session.scalar(select(EmailDelivery)).recipient_email == "lpr@example.test"


def test_google_sheet_column_k_maps_to_lpr_without_mixing_addresses() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:"); Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, _, _ = setup(session)
        config = GoogleSheetsConfig(spreadsheet_id="sheet", worksheet_name="unused", credentials_encrypted="unused")
        session.add(config); session.commit()
        worksheet = FakeWorksheet(); service = GoogleSheetsSyncService(session, config)
        service.sync_direction(direction, worksheet=worksheet)
        company_column = worksheet.rows[0].index("Почта")
        lpr_column = worksheet.rows[0].index("Почта", company_column + 1)
        assert lpr_column == 10  # Google Sheets column K, zero based.
        assert worksheet.rows[1][company_column] == "company@example.test"
        assert worksheet.rows[1][lpr_column] == "lpr@example.test"
        worksheet.rows[1][company_column] = "general-new@example.test"
        worksheet.rows[1][lpr_column] = "person-new@example.test"
        service.sync_direction(direction, worksheet=worksheet)
        assert company.company_email == "general-new@example.test"
        assert company.decision_maker_email == "person-new@example.test"
        worksheet.rows[1][lpr_column] = ""
        service.sync_direction(direction, worksheet=worksheet)
        assert company.company_email == "general-new@example.test"
        assert company.decision_maker_email == "person-new@example.test"


def test_google_sheet_prepare_uses_lpr_priority() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:"); Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, _, _ = setup(session)
        config = GoogleSheetsConfig(spreadsheet_id="sheet-trigger", worksheet_name="unused", credentials_encrypted="unused")
        session.add(config); session.commit()
        worksheet = FakeWorksheet(); GoogleSheetsSyncService(session, config).sync_direction(direction, worksheet=worksheet)
        action_column = worksheet.rows[0].index("Действие")
        worksheet.rows[1][action_column] = "Подготовить персональное КП"
        result = process_sheet_personalization_triggers(
            session, config, direction, Settings(public_base_url="https://leadflow.example"), worksheet=worksheet,
        )
        assert result["prepared"] == 1
        draft = session.scalar(select(SheetPersonalizationDraft))
        assert draft and draft.company_id == company.id and draft.recipient_email == "lpr@example.test"
