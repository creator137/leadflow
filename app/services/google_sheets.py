from __future__ import annotations

import json

import gspread
from google.oauth2.service_account import Credentials
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, EmailDelivery, GoogleSheetsConfig
from app.services.secrets import decrypt_secret

HEADERS = [
    "Дата добавления", "Направление", "Компания", "Город", "Адрес", "Email", "ИНН", "Сайт", "Телефон",
    "Контактное лицо", "Источник", "Ссылка на источник", "Статус", "Дата отправки", "Шаблон",
    "Почтовый аккаунт", "Открыто", "Клик", "Ответ", "Bounce", "Отписка",
]


def sync_companies(session: Session, config: GoogleSheetsConfig) -> dict[str, int]:
    info = json.loads(decrypt_secret(config.credentials_encrypted))
    credentials = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    worksheet = gspread.authorize(credentials).open_by_key(config.spreadsheet_id).worksheet(config.worksheet_name)
    companies = list(session.scalars(select(Company).order_by(Company.collected_at)))
    by_source_url = {company.source_url: company for company in companies if company.source_url}
    imported = 0
    try:
        existing_rows = worksheet.get_all_records()
    except Exception:
        existing_rows = []
    preserved_status: dict[str, str] = {}
    for row in existing_rows:
        source_url = str(row.get("Ссылка на источник") or "").strip()
        company = by_source_url.get(source_url)
        if not company:
            continue
        changed = False
        for sheet_key, attr in (("Направление", "category"), ("Email", "email"), ("Контактное лицо", "contact_person")):
            value = str(row.get(sheet_key) or "").strip() or None
            if value and value != getattr(company, attr):
                setattr(company, attr, value)
                changed = True
        sheet_status = str(row.get("Статус") or "").strip()
        if sheet_status:
            preserved_status[company.id] = sheet_status
            blocked = sheet_status.casefold() in {"blocked", "заблокирован", "не отправлять"}
            if blocked != company.manually_blocked:
                company.manually_blocked = blocked
                changed = True
        if changed:
            company.raw_data = {**(company.raw_data or {}), "google_sheets_status": sheet_status}
            imported += 1
    session.commit()
    deliveries = {
        d.company_id: d
        for d in session.scalars(select(EmailDelivery).order_by(EmailDelivery.sent_at.desc().nullslast()))
    }
    rows = [HEADERS]
    for company in companies:
        delivery = deliveries.get(company.id)
        rows.append([
            company.collected_at.isoformat(), company.category or "", company.company_name, company.city or "",
            company.address or "", company.email or "", company.inn or "", company.website or "", company.phone or "",
            company.contact_person or "", company.source, company.source_url or "",
            delivery.status if delivery else preserved_status.get(company.id, "new"),
            delivery.sent_at.isoformat() if delivery and delivery.sent_at else "", delivery.template_id if delivery else "",
            delivery.mailbox_id if delivery else "", bool(delivery and delivery.opened_at), bool(delivery and delivery.clicked_at),
            bool(delivery and delivery.replied_at), bool(delivery and delivery.bounced_at),
            bool(delivery and delivery.unsubscribed_at),
        ])
    worksheet.clear()
    if rows:
        worksheet.update(rows, "A1")
    return {"imported": imported, "exported": len(companies)}
