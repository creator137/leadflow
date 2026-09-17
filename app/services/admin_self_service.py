from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Campaign, Company, Direction, DirectionProposalTemplate, EmailDelivery, EmailEvent,
    EmailTemplate, MailAccount, SenderSettings, SheetPersonalizationDraft,
)
from app.services.recipients import resolve_recipient_email
from app.schemas import DirectionWizardCreate
from app.services.directions import create_direction, serialize_direction
from app.services.proposals import ensure_direction_email_template, ensure_proposal_template, get_sender_settings


def _plain_html(text: str) -> str:
    value = html.escape(text).replace("\n", "<br>")
    value = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"{{ \1 }}", value)
    return f'<div style="font-family:Arial,sans-serif;font-size:16px;line-height:1.55">{value}</div>'


def create_direction_bundle(session: Session, payload: DirectionWizardCreate) -> dict[str, Any]:
    mailbox = session.get(MailAccount, payload.mailbox_id)
    if not mailbox or not mailbox.active:
        raise ValueError("Выберите подключённый почтовый ящик.")
    direction = create_direction(session, payload.direction, commit=False)
    proposal = ensure_proposal_template(session, direction)
    proposal.subject = payload.proposal_subject
    proposal.greeting = payload.proposal_greeting
    proposal.main_body = payload.proposal_main_body
    proposal.extra_block = payload.proposal_extra_block
    proposal.cta = payload.proposal_cta
    proposal.signature = payload.proposal_signature_override
    proposal.ai_instruction = payload.proposal_ai_instruction
    proposal.ai_personalization_enabled = payload.proposal_ai_enabled
    ordinary = ensure_direction_email_template(session, direction)
    ordinary.name = f"Автоматическое письмо — {direction.name}"
    ordinary.subject_template = payload.email_template_subject
    ordinary.text_template = payload.email_template_text
    ordinary.html_template = _plain_html(payload.email_template_text)
    ordinary.active = True
    campaign = Campaign(
        name=payload.campaign_name, direction_id=direction.id, mailbox_id=mailbox.id,
        template_id=ordinary.id, schedule=payload.campaign_schedule,
        daily_limit=payload.campaign_daily_limit, run_limit=payload.campaign_daily_limit,
        status="running" if payload.campaign_enabled else "paused",
        active=payload.campaign_enabled,
    )
    session.add(campaign)
    session.commit()
    session.refresh(direction); session.refresh(proposal); session.refresh(ordinary); session.refresh(campaign)
    return {
        "direction": serialize_direction(session, direction).model_dump(mode="json"),
        "proposal_template_id": proposal.id,
        "email_template_id": ordinary.id,
        "campaign_id": campaign.id,
    }


def serialize_sender(row: SenderSettings) -> dict[str, Any]:
    return {
        "display_name": row.display_name, "position": row.position, "company_name": row.company_name,
        "phone": row.phone, "email": row.email, "website": row.website,
        "product_description": row.product_description, "logo_url": "/email-assets/bogorodsky-pryanik-logo.jpg",
        "signature_text": row.signature_text, "updated_at": row.updated_at,
    }


def update_sender_settings(session: Session, values: dict[str, Any]) -> SenderSettings:
    row = get_sender_settings(session)
    for key, value in values.items():
        setattr(row, key, value)
    session.commit(); session.refresh(row)
    return row


def campaign_next_run(campaign: Campaign) -> datetime | None:
    if not campaign.active or campaign.status != "running" or not campaign.schedule:
        return None
    now = datetime.now(timezone.utc)
    try:
        return CronTrigger.from_crontab(campaign.schedule, timezone="UTC").get_next_fire_time(None, now)
    except ValueError:
        return None


def serialize_campaign(session: Session, campaign: Campaign) -> dict[str, Any]:
    direction = session.get(Direction, campaign.direction_id) if campaign.direction_id else None
    template = session.get(EmailTemplate, campaign.template_id)
    mailbox = session.get(MailAccount, campaign.mailbox_id)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = len(list(session.scalars(select(EmailDelivery.id).where(
        EmailDelivery.campaign_id == campaign.id, EmailDelivery.sent_at >= today,
    ))))
    latest_failed = session.scalar(select(EmailDelivery).where(
        EmailDelivery.campaign_id == campaign.id,
        EmailDelivery.status.in_(["send_error", "failed", "bounced"]),
    ).order_by(EmailDelivery.updated_at.desc()).limit(1))
    return {
        "id": campaign.id, "name": campaign.name, "direction_id": campaign.direction_id,
        "direction_name": direction.name if direction else "—", "template_id": campaign.template_id,
        "template_name": template.name if template else "—", "mailbox_id": campaign.mailbox_id,
        "mailbox_name": mailbox.name if mailbox else "—", "schedule": campaign.schedule,
        "daily_limit": campaign.daily_limit, "run_limit": campaign.run_limit,
        "status": campaign.status, "active": campaign.active, "sent_today": sent_today,
        "last_run_at": campaign.last_run_at, "next_run_at": campaign_next_run(campaign),
        "last_queued": campaign.last_queued, "last_error": campaign.last_error or (latest_failed.error if latest_failed else None),
        "created_at": campaign.created_at, "updated_at": campaign.updated_at,
    }


STATUS_RU = {
    "preparing": "Подготовка", "draft": "Черновик", "ready": "Готово к отправке", "sent": "Отправлено",
    "queued": "В очереди", "sending": "Отправляется", "send_error": "Ошибка отправки", "failed": "Ошибка",
    "opened": "Открыто", "clicked": "Перешли по ссылке", "replied": "Получен ответ",
    "bounced": "Не доставлено", "unsubscribed": "Отписались",
}


EVENT_RU = {
    "queued": "Подготовлено", "sending": "Отправляется", "sent": "Отправлено", "opened": "Открыто",
    "clicked": "Перешли по ссылке", "replied": "Получен ответ", "bounced": "Не доставлено",
    "unsubscribed": "Отписались", "send_error": "Ошибка отправки", "failed": "Ошибка отправки",
}


def email_journal(
    session: Session, *, search: str | None = None, direction_id: str | None = None,
    status: str | None = None, mailbox_id: str | None = None, item_type: str | None = None, days: int | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    draft_query = select(SheetPersonalizationDraft, Company, Direction).join(
        Company, Company.id == SheetPersonalizationDraft.company_id
    ).join(Direction, Direction.id == SheetPersonalizationDraft.direction_id)
    delivery_query = select(EmailDelivery, Company).join(Company, Company.id == EmailDelivery.company_id)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        draft_query = draft_query.where(SheetPersonalizationDraft.updated_at >= cutoff)
        delivery_query = delivery_query.where(EmailDelivery.updated_at >= cutoff)
    if search:
        pattern = f"%{search.strip()}%"
        draft_query = draft_query.where(or_(Company.company_name.ilike(pattern), Company.company_email.ilike(pattern), Company.email.ilike(pattern)))
        delivery_query = delivery_query.where(or_(Company.company_name.ilike(pattern), EmailDelivery.recipient_email.ilike(pattern)))
    if direction_id:
        draft_query = draft_query.where(SheetPersonalizationDraft.direction_id == direction_id)
        delivery_query = delivery_query.where(EmailDelivery.direction_id == direction_id)
    if mailbox_id:
        draft_query = draft_query.where(SheetPersonalizationDraft.mailbox_id == mailbox_id)
        delivery_query = delivery_query.where(EmailDelivery.mailbox_id == mailbox_id)
    if item_type in {None, "proposal"}:
        for draft, company, direction in session.execute(draft_query):
            draft_status = "sent" if draft.status == "sent" else ("ready" if draft.status == "ready" else ("error" if draft.status == "error" else "draft"))
            if status and not (status == "errors" and draft_status == "error") and status != draft_status:
                continue
            mailbox = session.get(MailAccount, draft.mailbox_id) if draft.mailbox_id else None
            delivery = session.get(EmailDelivery, draft.delivery_id) if draft.delivery_id else None
            rows.append({
                "key": f"draft:{draft.id}", "kind": "proposal", "kind_label": "Персональное КП",
                "draft_id": draft.id, "delivery_id": draft.delivery_id, "company_id": company.id,
                "company": company.company_name, "direction_id": direction.id, "direction": direction.name,
                "recipient_email": (delivery.recipient_email if delivery else resolve_recipient_email(company)),
                "subject": draft.subject or "Без темы", "mailbox_id": draft.mailbox_id,
                "mailbox": mailbox.name if mailbox else "—", "status": draft_status,
                "status_label": STATUS_RU.get(draft_status, draft_status), "created_at": draft.created_at,
                "updated_at": draft.updated_at, "sent_at": delivery.sent_at if delivery else None,
                "read_only": draft.status == "sent",
            })
    if item_type in {None, "automatic"}:
        if status == "errors":
            delivery_query = delivery_query.where(EmailDelivery.status.in_(["send_error", "failed", "bounced"]))
        elif status:
            delivery_query = delivery_query.where(EmailDelivery.status == status)
        for delivery, company in session.execute(delivery_query.where(EmailDelivery.send_mode == "campaign")):
            direction = session.get(Direction, delivery.direction_id) if delivery.direction_id else None
            mailbox = session.get(MailAccount, delivery.mailbox_id)
            rows.append({
                "key": f"delivery:{delivery.id}", "kind": "automatic", "kind_label": "Автоматическое письмо",
                "draft_id": None, "delivery_id": delivery.id, "company_id": company.id,
                "company": company.company_name, "direction_id": delivery.direction_id,
                "direction": direction.name if direction else "—", "recipient_email": delivery.recipient_email,
                "subject": delivery.subject, "mailbox_id": delivery.mailbox_id,
                "mailbox": mailbox.name if mailbox else "—", "status": delivery.status,
                "status_label": STATUS_RU.get(delivery.status, delivery.status), "created_at": delivery.created_at,
                "updated_at": delivery.updated_at, "sent_at": delivery.sent_at, "read_only": True,
            })
    return sorted(rows, key=lambda x: x["updated_at"] or x["created_at"], reverse=True)[:500]


def delivery_timeline(session: Session, delivery_id: str) -> list[dict[str, Any]]:
    return [{
        "event": event.event_type, "label": EVENT_RU.get(event.event_type, event.event_type),
        "occurred_at": event.occurred_at,
    } for event in session.scalars(select(EmailEvent).where(
        EmailEvent.delivery_id == delivery_id,
    ).order_by(EmailEvent.occurred_at))]
