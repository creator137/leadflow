from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Company, CompanyDirection, Direction, EmailDelivery, SheetPersonalizationDraft
from app.services.google_sheets import _column_letter, _retry, active_sheets_config, discover_schema, worksheet_from_config
from app.services.mailing import send_delivery
from app.services.proposals import delivery_template_for_direction, ensure_proposal_template, finalize_proposal_delivery, prepare_proposal_draft
from app.services.sheet_personalization import PREVIEW_HEADER, STATUS_HEADER, _default_mailbox, _ensure_column, send_sheet_draft


def _direction(session: Session, company_id: str) -> Direction | None:
    return session.scalar(select(Direction).join(CompanyDirection, CompanyDirection.direction_id == Direction.id)
                          .where(CompanyDirection.company_id == company_id, Direction.archived_at.is_(None)).limit(1))


def _sheet_row(session: Session, company_id: str, direction: Direction, settings: Settings) -> tuple[Any, int, int, int]:
    config = active_sheets_config(session, settings)
    if not config:
        raise ValueError("Google Таблица не подключена")
    worksheet = _retry(lambda: worksheet_from_config(config, direction.sheet_tab))
    rows = _retry(worksheet.get_all_values)
    schema = discover_schema(rows)
    for row_number, row in enumerate(rows[schema.data_row - 1:], start=schema.data_row):
        if len(row) >= schema.leadflow_id_column and row[schema.leadflow_id_column - 1].strip() == company_id:
            header = rows[schema.header_row - 1]
            status = _ensure_column(worksheet, header, schema.header_row, STATUS_HEADER)
            preview = _ensure_column(worksheet, header, schema.header_row, PREVIEW_HEADER)
            return worksheet, row_number, status, preview
    raise ValueError("Компания не найдена в Google Таблице")


def _write(worksheet: Any, row: int, status_column: int, preview_column: int, status: str, preview: str = "") -> None:
    _retry(lambda: worksheet.batch_update([
        {"range": f"{_column_letter(status_column)}{row}", "values": [[status]]},
        {"range": f"{_column_letter(preview_column)}{row}", "values": [[preview]]},
    ]))


def _friendly(exc: Exception) -> str:
    text = str(exc).casefold()
    if "website" in text or "сайт" in text: return "Не указан сайт"
    if "recipient" in text or "email" in text: return "У компании не указан email для отправки"
    if "daily limit" in text or "дневн" in text: return "Достигнут дневной лимит"
    if "mailbox" in text or "почтов" in text: return "Почтовый ящик недоступен"
    if "suppres" in text or "запрет" in text: return "Для этого адреса действует запрет отправки"
    return str(exc) if any("а" <= char.casefold() <= "я" for char in str(exc)) else "Не удалось выполнить действие"


def execute_sheet_action(session: Session, company_id: str, action: str, settings: Settings) -> dict[str, object]:
    company = session.get(Company, company_id)
    if not company:
        raise ValueError("Компания не найдена")
    direction = _direction(session, company_id)
    if not direction:
        raise ValueError("Направление компании не найдено")
    worksheet, row, status_column, preview_column = _sheet_row(session, company_id, direction, settings)
    config = active_sheets_config(session, settings)
    template, mailbox = delivery_template_for_direction(session, direction), _default_mailbox(session)
    if not mailbox: raise ValueError("Почтовый ящик недоступен")
    proposal_template = ensure_proposal_template(session, direction)
    command_key = hashlib.sha256(
        f"sheet-action-v2:{config.id}:{direction.id}:{company_id}:{proposal_template.version}".encode()
    ).hexdigest()
    draft = session.scalar(select(SheetPersonalizationDraft).where(SheetPersonalizationDraft.command_key == command_key).with_for_update())
    try:
        if action == "prepare":
            if not company.website: raise ValueError("Не указан сайт")
            if draft and draft.status == "ready":
                preview_url = f"{settings.public_base_url.rstrip('/')}/?company={company.id}&draft={draft.id}#companies"
                _write(worksheet, row, status_column, preview_column, "КП подготовлено", preview_url)
                return {"status": "КП подготовлено", "preview_url": preview_url, "duplicate": True}
            if not draft:
                draft = SheetPersonalizationDraft(company_id=company.id, direction_id=direction.id, config_id=config.id,
                    template_id=template.id, mailbox_id=mailbox.id, command_key=command_key, status="preparing")
                session.add(draft)
                try: session.commit()
                except IntegrityError:
                    session.rollback()
                    draft = session.scalar(select(SheetPersonalizationDraft).where(SheetPersonalizationDraft.command_key == command_key))
            _write(worksheet, row, status_column, preview_column, "Подготовка...")
            draft = prepare_proposal_draft(
                session, company, settings, mailbox=mailbox, config=config, command_key=command_key,
            )
            preview_url = f"{settings.public_base_url.rstrip('/')}/?company={company.id}&draft={draft.id}#companies"
            _write(worksheet, row, status_column, preview_column, "КП подготовлено", preview_url)
            return {"status": "КП подготовлено", "preview_url": preview_url, "duplicate": False}
        if not draft or draft.status not in {"ready", "sent"}: raise ValueError("Сначала подготовьте КП")
        already_sent = bool(draft.delivery_id)
        delivery = send_sheet_draft(session, draft.id, settings)
        if delivery.status == "queued":
            send_delivery(session, delivery, mailbox)
            session.refresh(delivery)
        if delivery.status != "sent":
            raise ValueError(delivery.error or "Почтовый ящик недоступен")
        finalize_proposal_delivery(session, draft, delivery)
        preview_url = f"{settings.public_base_url.rstrip('/')}/?company={company.id}&draft={draft.id}#companies"
        _write(worksheet, row, status_column, preview_column, "Отправлено", preview_url)
        return {"status": "Отправлено", "message_id": delivery.message_id, "duplicate": already_sent}
    except Exception as exc:
        session.rollback()
        message = _friendly(exc)
        _write(worksheet, row, status_column, preview_column, "Ошибка", message)
        raise ValueError(message) from exc
