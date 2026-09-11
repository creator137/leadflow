from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Company, CompanyDirection, Direction, EmailDelivery, EmailTemplate, GoogleSheetsConfig,
    MailAccount, SheetPersonalizationDraft,
)
from app.services.google_sheets import _column_letter, _normalized, _retry, discover_schema, worksheet_from_config
from app.services.personalized import prepare_personalization, send_personalized
from app.services.user_errors import human_error


SHEET_PERSONALIZATION_COMMAND = "подготовить персональное кп"
STATUS_HEADER = "Статус персонального КП"
PREVIEW_HEADER = "Предпросмотр КП"


def _ensure_column(worksheet: Any, header: list[str], header_row: int, title: str) -> int:
    wanted = _normalized(title)
    for index, value in enumerate(header, 1):
        if _normalized(value) == wanted:
            return index
    column = len(header) + 1
    if getattr(worksheet, "col_count", column) < column:
        _retry(lambda: worksheet.add_cols(column - worksheet.col_count))
    _retry(lambda: worksheet.update(range_name=f"{_column_letter(column)}{header_row}", values=[[title]]))
    header.append(title)
    return column


def _default_template(session: Session, direction: Direction) -> EmailTemplate | None:
    return session.scalar(select(EmailTemplate).where(
        EmailTemplate.active.is_(True), EmailTemplate.direction_id == direction.id,
    ).order_by(EmailTemplate.created_at).limit(1)) or session.scalar(select(EmailTemplate).where(
        EmailTemplate.active.is_(True),
    ).order_by(EmailTemplate.created_at).limit(1))


def _default_mailbox(session: Session) -> MailAccount | None:
    return session.scalar(select(MailAccount).where(MailAccount.active.is_(True)).order_by(MailAccount.created_at).limit(1))


def _sheet_status(draft: SheetPersonalizationDraft, delivery: EmailDelivery | None) -> str:
    if delivery:
        return {
            "queued": "В очереди", "sending": "Отправляется", "sent": "Отправлено",
            "opened": "Открыто", "clicked": "Перешли по ссылке", "replied": "Получен ответ",
            "bounced": "Не доставлено", "unsubscribed": "Отписались", "send_error": "Ошибка",
        }.get(delivery.status, "Ошибка")
    return {"preparing": "Подготовка...", "ready": "Готово к отправке", "error": "Ошибка"}.get(draft.status, "Ошибка")


def process_sheet_personalization_triggers(
    session: Session, config: GoogleSheetsConfig, direction: Direction, settings: Settings, *, worksheet: Any = None,
) -> dict[str, int]:
    """Prepare, but never send, a personalized proposal requested in a Sheet row."""
    worksheet = worksheet or _retry(lambda: worksheet_from_config(config, direction.sheet_tab))
    rows = _retry(worksheet.get_all_values)
    if not rows:
        return {"commands": 0, "prepared": 0, "reused": 0, "errors": 0}
    schema = discover_schema(rows)
    action_column = schema.fields.get("action")
    if not action_column:
        return {"commands": 0, "prepared": 0, "reused": 0, "errors": 0}
    header = rows[schema.header_row - 1]
    status_column = _ensure_column(worksheet, header, schema.header_row, STATUS_HEADER)
    preview_column = _ensure_column(worksheet, header, schema.header_row, PREVIEW_HEADER)
    updates: list[dict[str, Any]] = []
    counters = {"commands": 0, "prepared": 0, "reused": 0, "errors": 0}

    for row_number, row in enumerate(rows[schema.data_row - 1:], start=schema.data_row):
        action = row[action_column - 1] if len(row) >= action_column else ""
        if _normalized(action) != SHEET_PERSONALIZATION_COMMAND:
            continue
        counters["commands"] += 1
        company_id = row[schema.leadflow_id_column - 1].strip() if len(row) >= schema.leadflow_id_column else ""
        company = session.get(Company, company_id) if company_id else None
        linked = company and session.scalar(select(CompanyDirection.company_id).where(
            CompanyDirection.company_id == company.id, CompanyDirection.direction_id == direction.id,
        ))
        template = _default_template(session, direction)
        mailbox = _default_mailbox(session)
        command_key = hashlib.sha256(f"{config.id}:{direction.id}:{company_id}:{SHEET_PERSONALIZATION_COMMAND}".encode()).hexdigest()
        draft = session.scalar(select(SheetPersonalizationDraft).where(SheetPersonalizationDraft.command_key == command_key))
        if not company or not linked or not template:
            message = "Компания не найдена по LeadFlow ID." if not company or not linked else "Сначала создайте шаблон письма."
            updates.append({"range": f"{_column_letter(status_column)}{row_number}", "values": [["Ошибка"]]})
            updates.append({"range": f"{_column_letter(preview_column)}{row_number}", "values": [[message]]})
            counters["errors"] += 1
            continue
        if draft is None:
            draft = SheetPersonalizationDraft(
                company_id=company.id, direction_id=direction.id, config_id=config.id,
                template_id=template.id, mailbox_id=mailbox.id if mailbox else None,
                command_key=command_key, status="preparing",
            )
            session.add(draft)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                draft = session.scalar(select(SheetPersonalizationDraft).where(SheetPersonalizationDraft.command_key == command_key))
        if draft.status == "preparing" and not draft.request_key:
            preparing_cell = f"{_column_letter(status_column)}{row_number}"
            _retry(lambda: worksheet.update(range_name=preparing_cell, values=[["Подготовка..."]]))
            try:
                preview = prepare_personalization(session, company, template, settings, account=mailbox)
                draft.request_key = str(preview["request_key"])
                draft.subject = str(preview["subject"])
                draft.html_body = str(preview["html_body"])
                draft.text_body = str(preview["text_body"])
                draft.facts = list(preview.get("facts") or [])
                draft.status, draft.error = "ready", None
                session.commit()
                counters["prepared"] += 1
            except Exception as exc:
                session.rollback()
                draft = session.scalar(select(SheetPersonalizationDraft).where(SheetPersonalizationDraft.command_key == command_key))
                if draft:
                    draft.status, draft.error = "error", human_error(exc)
                    session.commit()
                counters["errors"] += 1
        else:
            counters["reused"] += 1
        delivery = session.get(EmailDelivery, draft.delivery_id) if draft and draft.delivery_id else None
        status = _sheet_status(draft, delivery) if draft else "Ошибка"
        preview_url = ""
        if draft and draft.status == "ready":
            preview_url = f"{settings.public_base_url.rstrip('/')}/?company={company.id}&draft={draft.id}#companies"
        elif draft and draft.error:
            preview_url = draft.error
        updates.append({"range": f"{_column_letter(status_column)}{row_number}", "values": [[status]]})
        updates.append({"range": f"{_column_letter(preview_column)}{row_number}", "values": [[preview_url]]})
    if updates:
        _retry(lambda: worksheet.batch_update(updates))
    return counters


def send_sheet_draft(
    session: Session, draft_id: str, settings: Settings, *, overrides: dict[str, str | None] | None = None,
) -> EmailDelivery:
    draft = session.scalar(select(SheetPersonalizationDraft).where(
        SheetPersonalizationDraft.id == draft_id,
    ).with_for_update())
    if not draft or draft.status != "ready" or not draft.request_key:
        raise ValueError("Персональное письмо ещё не готово.")
    if draft.delivery_id:
        delivery = session.get(EmailDelivery, draft.delivery_id)
        if delivery:
            return delivery
    company = session.get(Company, draft.company_id)
    template = session.get(EmailTemplate, draft.template_id)
    mailbox = session.get(MailAccount, draft.mailbox_id) if draft.mailbox_id else None
    if not company or not template or not mailbox:
        raise ValueError("Почтовый ящик или шаблон не настроен.")
    delivery = send_personalized(
        session, company, mailbox, template, draft.request_key, settings,
        overrides=overrides or {"subject": draft.subject, "html_body": draft.html_body, "text_body": draft.text_body},
    )
    draft.delivery_id = delivery.id
    session.commit()
    return delivery


def serialize_draft(draft: SheetPersonalizationDraft) -> dict[str, Any]:
    return {
        "id": draft.id, "company_id": draft.company_id, "template_id": draft.template_id,
        "mailbox_id": draft.mailbox_id, "status": draft.status, "subject": draft.subject,
        "html_body": draft.html_body, "text_body": draft.text_body, "facts": draft.facts,
        "request_key": draft.request_key, "delivery_id": draft.delivery_id,
        "error": draft.error, "created_at": draft.created_at,
    }
