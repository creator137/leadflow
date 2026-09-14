from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import (
    AISettings, Company, CompanyDirection, Direction, DirectionProposalTemplate, EmailDelivery,
    EmailTemplate, GoogleSheetsConfig, MailAccount, SheetPersonalizationDraft, WebsiteAnalysis,
)
from app.services.proposals import (
    ProposalAIResult, _validated_evidence, default_copy, ensure_proposal_template,
    ensure_all_proposal_templates, finalize_proposal_delivery, prepare_proposal_draft, render_proposal, send_proposal_draft,
    update_proposal_draft,
)
from app.services.secrets import encrypt_secret


def setup(session: Session, direction_name: str = "Рестораны"):
    direction = Direction(name=direction_name, slug=direction_name.casefold(), sheet_tab=direction_name)
    company = Company(
        source="yandex_maps", company_name="Тестовая организация", city="Москва",
        company_email="test@example.test", normalized_name="тестовая организация", raw_data={},
    )
    session.add_all([direction, company]); session.flush()
    session.add(CompanyDirection(company_id=company.id, direction_id=direction.id))
    template = EmailTemplate(
        name="Системная связь", direction_id=direction.id, subject_template="Тема",
        html_template="<p>Текст</p>", text_template="Текст", active=True,
    )
    mailbox = MailAccount(
        name="Тест", from_email="sender@example.test", smtp_host="smtp.example.test", smtp_port=465,
        smtp_login="sender@example.test", smtp_password_encrypted=encrypt_secret("unused"), smtp_security="ssl",
        imap_host="imap.example.test", imap_port=993, imap_login="sender@example.test",
        imap_password_encrypted=encrypt_secret("unused"), imap_security="ssl", active=True,
    )
    session.add_all([template, mailbox, AISettings(id="default", personalization_enabled=False)])
    session.commit()
    return direction, company, template, mailbox


def test_each_direction_gets_distinct_business_copy() -> None:
    restaurant = default_copy("Рестораны")
    hotel = default_copy("Отели")
    event = default_copy("Event-агентства")
    assert restaurant.subject != hotel.subject != event.subject
    assert "банкет" in restaurant.main_body
    assert "welcome" in hotel.main_body
    assert "спикер" in event.main_body


def test_expected_direction_templates_are_seeded_paused_without_jobs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ensure_all_proposal_templates(session)
        directions = list(session.scalars(select(Direction)))
        assert {x.name for x in directions} >= {
            "Рестораны", "Кафе", "Отели", "Event-агентства", "Кейтеринг", "Туроператоры", "Школы",
        }
        assert all(not x.active for x in directions)
        assert session.query(DirectionProposalTemplate).count() == len(directions)


def test_ai_json_rejects_html_and_fact_without_evidence() -> None:
    with pytest.raises(ValidationError):
        ProposalAIResult.model_validate({
            "company_summary": None, "relevant_facts": [], "personalized_intro": "<b>Привет</b>",
            "relevance_paragraph": None, "suggested_use_cases": [], "personalized_cta_hint": None,
            "confidence": 0.5,
        })
    with pytest.raises(ValidationError):
        ProposalAIResult.model_validate({
            "company_summary": None,
            "relevant_facts": [{"fact": "Есть банкеты", "evidence": "", "source_url": "https://example.test"}],
            "personalized_intro": None, "relevance_paragraph": None, "suggested_use_cases": [],
            "personalized_cta_hint": None, "confidence": 0.5,
        })


def test_hallucinated_fact_is_rejected_when_quote_not_on_source_page() -> None:
    result = ProposalAIResult.model_validate({
        "company_summary": "Ресторан", "relevant_facts": [{
            "fact": "Проводят свадьбы", "evidence": "Организуем свадьбы", "source_url": "https://example.test/about",
        }], "personalized_intro": "Увидели информацию о свадьбах.", "relevance_paragraph": None,
        "suggested_use_cases": [], "personalized_cta_hint": None, "confidence": 0.8,
    })
    analysis = WebsiteAnalysis(
        company_id="company", website="https://example.test", content_hash="a" * 64,
        page_blocks=[{"url": "https://example.test/about", "text": "Ресторан открыт ежедневно."}],
    )
    assert _validated_evidence(result, analysis) == []


def test_draft_is_editable_without_sending_and_renderer_has_logo_and_plain_fallback() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, _, mailbox = setup(session)
        business = ensure_proposal_template(session, direction); session.commit()
        draft = prepare_proposal_draft(session, company, Settings(public_base_url="https://leadflow.example"), mailbox=mailbox)
        assert draft.status == "ready"
        assert session.query(EmailDelivery).count() == 0
        assert draft.proposal_template_id == business.id
        updated = update_proposal_draft(session, draft, {"cta": "Напишите, если хотите посмотреть примеры."}, Settings(public_base_url="https://leadflow.example"))
        rendered = render_proposal(updated, company, Settings(public_base_url="https://leadflow.example"))
        assert "Напишите, если хотите" in rendered["text_body"]
        assert 'alt="Богородский пряник"' in rendered["html_body"]
        assert "https://leadflow.example/email-assets/" in rendered["html_body"]
        assert "<html" not in rendered["text_body"]


def test_legacy_empty_draft_preview_uses_saved_snapshot() -> None:
    company = Company(
        source="two_gis", company_name="Старый черновик", normalized_name="старый черновик", raw_data={},
    )
    draft = SheetPersonalizationDraft(
        company_id="company", direction_id="direction", template_id="template",
        command_key="legacy", status="ready", subject="Старая тема",
        html_snapshot="<html><body>Сохранённый предпросмотр</body></html>",
    )
    rendered = render_proposal(draft, company, Settings(public_base_url="https://leadflow.example"))
    assert rendered["html_body"] == draft.html_snapshot
    assert rendered["subject"] == "Старая тема"


def test_send_uses_saved_draft_is_idempotent_and_sent_snapshot_is_stable() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction, company, _, mailbox = setup(session)
        draft = prepare_proposal_draft(session, company, Settings(public_base_url="https://leadflow.example"), mailbox=mailbox)
        update_proposal_draft(session, draft, {"main_body": "Сохранённый пользовательский текст."}, Settings(public_base_url="https://leadflow.example"))
        first = send_proposal_draft(session, draft, Settings(public_base_url="https://leadflow.example"))
        second = send_proposal_draft(session, draft, Settings(public_base_url="https://leadflow.example"))
        assert first.id == second.id
        assert session.query(EmailDelivery).count() == 1
        assert "Сохранённый пользовательский текст" in first.html_body
        first.status = "sent"; session.commit()
        finalize_proposal_delivery(session, draft, first)
        sent_snapshot = draft.sent_html_snapshot
        business = session.get(DirectionProposalTemplate, draft.proposal_template_id)
        business.main_body = "Новый текст шаблона"; business.version += 1; session.commit()
        assert render_proposal(draft, company, Settings(public_base_url="https://leadflow.example"))["html_body"] == sent_snapshot


def test_google_prepare_creates_draft_but_never_delivery() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _, company, _, mailbox = setup(session)
        config = GoogleSheetsConfig(spreadsheet_id="sheet", worksheet_name="Лист", credentials_encrypted="unused")
        session.add(config); session.commit()
        draft = prepare_proposal_draft(
            session, company, Settings(public_base_url="https://leadflow.example"), mailbox=mailbox,
            config=config, command_key="google-sheet-click",
        )
        assert draft.command_key == "google-sheet-click"
        assert draft.status == "ready"
        assert session.query(EmailDelivery).count() == 0


def test_project_logo_exists_and_is_not_empty() -> None:
    logo = Path(__file__).parents[1] / "logo.jpg"
    assert logo.exists() and logo.stat().st_size > 10_000
