import asyncio
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.main import create_campaign, pause_campaign, resume_campaign, update_campaign
from app.models import (
    AISettings, Campaign, Company, CompanyDirection, Direction, DirectionProposalTemplate,
    DirectionAttachment, EmailDelivery, EmailEvent, EmailTemplate, MailAccount, SenderSettings,
)
from app.schemas import CampaignCreate, CampaignUpdate, DirectionUpdate, DirectionWizardCreate
from app.services.admin_self_service import create_direction_bundle, delivery_timeline, email_journal, update_sender_settings
from app.services.directions import update_direction
from app.services.attachments import save_attachment
from app.services.mailing import build_email_message, queue_campaign
from app.services.proposals import ensure_direction_email_template, ensure_proposal_template, prepare_proposal_draft, render_proposal, send_proposal_draft, update_proposal_draft
from app.services.secrets import encrypt_secret


def session_with_mailbox() -> tuple[Session, MailAccount]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    mailbox = MailAccount(
        name="Основная", from_email="sender@example.test", from_name="Отправитель",
        smtp_host="smtp.example.test", smtp_port=465, smtp_login="sender@example.test",
        smtp_password_encrypted=encrypt_secret("unused"), smtp_security="ssl",
        imap_host="imap.example.test", imap_port=993, imap_login="sender@example.test",
        imap_password_encrypted=encrypt_secret("unused"), imap_security="ssl", active=True,
    )
    session.add_all([mailbox, AISettings(id="default", personalization_enabled=False)])
    session.commit()
    return session, mailbox


def wizard_payload(mailbox_id: str) -> DirectionWizardCreate:
    return DirectionWizardCreate.model_validate({
        "direction": {
            "name": "Фитнес-клубы", "sheet_tab": "Фитнес-клубы", "active": True,
            "limit_new": 15, "schedule": "0 5 * * *", "email_enrichment_enabled": True,
            "ai_enrichment_enabled": True,
            "queries": [{"query": "фитнес клуб"}, {"query": "спортивный клуб"}],
            "locations": [{"city": "Москва", "region": "Москва"}],
            "sources": ["yandex_maps", "two_gis"],
        },
        "proposal_subject": "Подарки для гостей клуба", "proposal_greeting": "Здравствуйте!",
        "proposal_main_body": "Базовый текст КП", "proposal_extra_block": "",
        "proposal_cta": "Могу прислать примеры.", "proposal_signature_override": "",
        "proposal_ai_instruction": "Учитывай только подтверждённые мероприятия клуба.",
        "proposal_ai_enabled": True, "email_template_subject": "Для {{company_name}}",
        "email_template_text": "Здравствуйте, {{company_name}}!\n\n{{sender_name}}\n{{sender_phone}}",
        "mailbox_id": mailbox_id, "campaign_name": "Фитнес — автоматическая",
        "campaign_daily_limit": 12, "campaign_schedule": None, "campaign_enabled": False,
    })


def test_wizard_creates_search_proposal_non_ai_template_and_campaign_without_duplicates() -> None:
    session, mailbox = session_with_mailbox()
    try:
        result = create_direction_bundle(session, wizard_payload(mailbox.id))
        direction = session.get(Direction, result["direction"]["id"])
        proposal = session.get(DirectionProposalTemplate, result["proposal_template_id"])
        ordinary = session.get(EmailTemplate, result["email_template_id"])
        campaign = session.get(Campaign, result["campaign_id"])
        assert direction.automatic_template_id == ordinary.id
        assert proposal.direction_id == direction.id and proposal.ai_instruction.startswith("Учитывай")
        assert ordinary.direction_id == direction.id and "{{company_name}}" in ordinary.text_template
        assert campaign.template_id == ordinary.id and campaign.mailbox_id == mailbox.id
        assert campaign.active is False and campaign.status == "paused"
        assert len(result["direction"]["queries"]) == 2 and len(result["direction"]["sources"]) == 2

        update_direction(session, direction, DirectionUpdate(limit_new=25))
        ensure_proposal_template(session, direction); ensure_direction_email_template(session, direction); session.commit()
        assert session.query(DirectionProposalTemplate).filter_by(direction_id=direction.id).count() == 1
        assert session.query(EmailTemplate).filter_by(direction_id=direction.id).count() == 1
    finally:
        session.close()


def test_campaign_crud_pause_resume_and_non_ai_queue(monkeypatch) -> None:
    session, mailbox = session_with_mailbox()
    try:
        result = create_direction_bundle(session, wizard_payload(mailbox.id))
        direction = session.get(Direction, result["direction"]["id"])
        template = session.get(EmailTemplate, result["email_template_id"])
        company = Company(
            source="manual", company_name="Тестовая компания", city="Москва",
            company_email="client@example.test", normalized_name="тестовая компания", raw_data={},
        )
        session.add(company); session.flush(); session.add(CompanyDirection(company_id=company.id, direction_id=direction.id)); session.commit()
        created = create_campaign(CampaignCreate(
            name="Тест CRUD", direction_id=direction.id, mailbox_id=mailbox.id, template_id=template.id,
            daily_limit=1, run_limit=1, status="paused", active=False,
        ), session)
        updated = update_campaign(created["id"], CampaignUpdate(name="Тест CRUD 2", daily_limit=2), session)
        assert updated["name"] == "Тест CRUD 2" and updated["daily_limit"] == 2
        assert resume_campaign(created["id"], session).active is True
        assert pause_campaign(created["id"], session).active is False
        monkeypatch.setattr("app.services.deepseek.DeepSeekClient.generate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("DeepSeek must not run")))
        campaign = session.get(Campaign, created["id"]); campaign.active = True; campaign.status = "running"; session.commit()
        assert queue_campaign(session, campaign, Settings(public_base_url="https://lead.test"))["queued"] == 1
        delivery = session.scalar(select(EmailDelivery).where(EmailDelivery.campaign_id == campaign.id))
        assert delivery.send_mode == "campaign" and "Тестовая компания" in delivery.subject
    finally:
        session.close()


def test_campaign_respects_daily_limit_and_suppression() -> None:
    # Daily-limit and suppression behavior is exercised in test_email_outreach;
    # this assertion makes the self-service campaign retain those same defaults.
    session, mailbox = session_with_mailbox()
    try:
        result = create_direction_bundle(session, wizard_payload(mailbox.id))
        campaign = session.get(Campaign, result["campaign_id"])
        assert campaign.daily_limit == 12 and campaign.run_limit == 12 and campaign.cooldown_days == 30
    finally:
        session.close()


def test_sender_settings_are_inherited_only_by_new_drafts_and_sent_snapshot_is_stable() -> None:
    session, mailbox = session_with_mailbox()
    try:
        result = create_direction_bundle(session, wizard_payload(mailbox.id))
        direction = session.get(Direction, result["direction"]["id"])
        company = Company(source="manual", company_name="Клуб", company_email="club@example.test", normalized_name="клуб", raw_data={})
        session.add(company); session.flush(); session.add(CompanyDirection(company_id=company.id, direction_id=direction.id)); session.commit()
        update_sender_settings(session, {
            "display_name": "Ирина", "position": "Менеджер", "company_name": "Фабрика",
            "phone": "+7 900 000-00-00", "email": "sales@example.test", "website": "https://example.test",
            "product_description": "Описание", "signature_text": "С уважением,\nИрина",
        })
        first = prepare_proposal_draft(session, company, Settings(public_base_url="https://lead.test"), mailbox=mailbox)
        assert first.signature == "С уважением,\nИрина"
        original = render_proposal(first, company, Settings(public_base_url="https://lead.test"))["html_body"]
        first.status = "sent"; first.sent_html_snapshot = original; session.commit()
        update_sender_settings(session, {
            "display_name": "Ольга", "position": "Менеджер", "company_name": "Фабрика",
            "phone": "+7 911 111-11-11", "email": "new@example.test", "website": "https://new.example.test",
            "product_description": "Новое описание", "signature_text": "С уважением,\nОльга",
        })
        assert render_proposal(first, company, Settings(public_base_url="https://lead.test"))["html_body"] == original
    finally:
        session.close()


def test_journal_filters_read_only_sent_and_uses_existing_event_timeline() -> None:
    session, mailbox = session_with_mailbox()
    try:
        result = create_direction_bundle(session, wizard_payload(mailbox.id))
        direction = session.get(Direction, result["direction"]["id"])
        company = Company(source="manual", company_name="Журнал Тест", company_email="journal@example.test", normalized_name="журнал тест", raw_data={})
        session.add(company); session.flush(); session.add(CompanyDirection(company_id=company.id, direction_id=direction.id)); session.commit()
        draft = prepare_proposal_draft(session, company, Settings(public_base_url="https://lead.test"), mailbox=mailbox)
        ready = email_journal(session, search="Журнал", status="ready", item_type="proposal")
        assert len(ready) == 1 and ready[0]["read_only"] is False
        delivery = EmailDelivery(
            company_id=company.id, direction_id=direction.id, mailbox_id=mailbox.id,
            template_id=result["email_template_id"], recipient_email=company.company_email,
            subject="Снимок", html_body="<p>Снимок</p>", text_body="Снимок", send_mode="campaign",
            status="sent", tracking_token="t", unsubscribe_token="u",
        )
        session.add(delivery); session.flush(); session.add_all([
            EmailEvent(delivery_id=delivery.id, company_id=company.id, event_type="queued"),
            EmailEvent(delivery_id=delivery.id, company_id=company.id, event_type="sent"),
        ]); session.commit()
        sent = email_journal(session, search="journal@example.test", status="sent", item_type="automatic")
        assert len(sent) == 1 and sent[0]["read_only"] is True
        assert [x["label"] for x in delivery_timeline(session, delivery.id)] == ["Подготовлено", "Отправлено"]
    finally:
        session.close()


def test_manager_email_ui_and_no_secret_fields_in_frontend() -> None:
    root = Path(__file__).parents[1]
    script = (root / "app/static/self_service.js").read_text()
    html_page = (root / "app/static/index.html").read_text()
    assert "Куда пересылать ответы клиентов" in script
    assert "forward_replies_to" in script
    assert "smtp_password_encrypted" not in script + html_page
    assert "DeepSeek" not in (root / "app/static/self_service.js").read_text().split("Автоматическая рассылка")[1].split("function loadCampaigns")[0]


def test_direction_attachments_are_snapshotted_and_recipient_is_editable(tmp_path: Path, monkeypatch) -> None:
    session, mailbox = session_with_mailbox()
    try:
        result = create_direction_bundle(session, wizard_payload(mailbox.id))
        direction = session.get(Direction, result["direction"]["id"])
        settings = Settings(public_base_url="https://lead.test", attachment_storage_dir=str(tmp_path))
        uploaded = UploadFile(filename="Презентация.pdf", file=BytesIO(b"%PDF-1.4 test presentation\n%%EOF"))
        attachment = asyncio.run(save_attachment(session, settings, direction.id, uploaded))
        company = Company(source="manual", company_name="Получатель", company_email="old@example.test", normalized_name="получатель", raw_data={})
        session.add(company); session.flush(); session.add(CompanyDirection(company_id=company.id, direction_id=direction.id)); session.commit()
        draft = prepare_proposal_draft(session, company, settings, mailbox=mailbox)
        assert draft.recipient_email == "old@example.test"
        assert draft.attachments_snapshot[0]["filename"] == "Презентация.pdf"
        update_proposal_draft(session, draft, {"recipient_email": "new@example.test"}, settings)
        attachment.active = False; session.commit()
        delivery = send_proposal_draft(session, draft, settings)
        assert delivery.recipient_email == "new@example.test"
        assert delivery.attachments_snapshot[0]["sha256"] == attachment.sha256
        monkeypatch.setattr("app.services.mailing.get_settings", lambda: settings)
        message = build_email_message(delivery, mailbox)
        assert any(part.get_filename() == "Презентация.pdf" for part in message.iter_attachments())
    finally:
        session.close()


def test_attachment_validation_rejects_executable(tmp_path: Path) -> None:
    session, mailbox = session_with_mailbox()
    try:
        result = create_direction_bundle(session, wizard_payload(mailbox.id))
        upload = UploadFile(filename="program.exe", file=BytesIO(b"MZ"))
        try:
            asyncio.run(save_attachment(session, Settings(attachment_storage_dir=str(tmp_path)), result["direction"]["id"], upload))
        except ValueError as exc:
            assert str(exc) == "Формат файла не поддерживается."
        else:
            raise AssertionError("Executable attachment must be rejected")
        assert session.query(DirectionAttachment).count() == 0
    finally:
        session.close()
