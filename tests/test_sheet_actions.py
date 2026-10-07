import hashlib
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import (
    Company, CompanyDirection, Direction, DirectionProposalTemplate, EmailTemplate,
    GoogleSheetsConfig, MailAccount, SheetPersonalizationDraft,
)
from app.services.secrets import encrypt_secret
from app.services.sheet_actions import execute_sheet_action


def test_send_action_refreshes_recipient_from_current_sheet_row(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        direction = Direction(name="Кейтеринг", slug="кейтеринг", sheet_tab="Кейтеринг")
        company = Company(
            source="manual", company_name="Получатель", company_email="main@example.test",
            normalized_name="получатель", raw_data={},
        )
        mailbox = MailAccount(
            name="Основная", from_email="sender@example.test", from_name="Отправитель",
            smtp_host="smtp.example.test", smtp_login="sender@example.test",
            smtp_password_encrypted=encrypt_secret("unused"), imap_host="imap.example.test",
            imap_login="sender@example.test", imap_password_encrypted=encrypt_secret("unused"),
            active=True, is_primary=True,
        )
        config = GoogleSheetsConfig(
            spreadsheet_id="sheet", worksheet_name="Кейтеринг", credentials_encrypted="unused",
        )
        session.add_all([direction, company, mailbox, config]); session.flush()
        template = EmailTemplate(
            name="Кейтеринг", direction_id=direction.id, subject_template="Тема",
            html_template="<p>Текст</p>", text_template="Текст",
        )
        proposal = DirectionProposalTemplate(
            direction_id=direction.id, version=1, subject="Тема", greeting="Здравствуйте!",
            main_body="Предложение", extra_block="", cta="Ответьте нам", signature="Подпись",
            ai_instruction="Только факты", ai_personalization_enabled=False,
        )
        session.add_all([template, proposal]); session.flush()
        session.add(CompanyDirection(company_id=company.id, direction_id=direction.id))
        command_key = hashlib.sha256(
            f"sheet-action-v2:{config.id}:{direction.id}:{company.id}:{proposal.version}".encode()
        ).hexdigest()
        draft = SheetPersonalizationDraft(
            company_id=company.id, direction_id=direction.id, config_id=config.id,
            template_id=template.id, proposal_template_id=proposal.id, mailbox_id=mailbox.id,
            recipient_email="main@example.test", command_key=command_key, status="ready",
        )
        session.add(draft); session.commit()

        monkeypatch.setattr(
            "app.services.sheet_actions._sheet_row",
            lambda *_: (object(), ["row"], object(), 3, 12, 13),
        )
        monkeypatch.setattr("app.services.sheet_actions.active_sheets_config", lambda *_: config)
        monkeypatch.setattr(
            "app.services.sheet_actions.import_sheet_recipient_fields",
            lambda *_: {"company_email": "main@example.test", "decision_maker_email": "lpr@example.test"},
        )
        monkeypatch.setattr("app.services.sheet_actions.delivery_template_for_direction", lambda *_: template)
        monkeypatch.setattr("app.services.sheet_actions._default_mailbox", lambda *_: mailbox)
        monkeypatch.setattr("app.services.sheet_actions.ensure_proposal_template", lambda *_: proposal)
        monkeypatch.setattr("app.services.sheet_actions._write", lambda *_: None)
        sent_to = []

        def fake_send(current_session, draft_id, _settings):
            current = current_session.get(SheetPersonalizationDraft, draft_id)
            sent_to.append(current.recipient_email)
            return SimpleNamespace(
                status="sent", message_id="<sent@example.test>",
                html_body="<p>Текст</p>", text_body="Текст",
            )

        monkeypatch.setattr("app.services.sheet_actions.send_sheet_draft", fake_send)

        result = execute_sheet_action(session, company.id, "send", Settings(public_base_url="https://lead.test"))

        session.refresh(draft)
        assert result["status"] == "Отправлено"
        assert sent_to == ["lpr@example.test"]
        assert draft.recipient_email == "lpr@example.test"

