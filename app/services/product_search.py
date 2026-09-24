"""Separate supplier/product research workspace.

The service intentionally reuses only URL discovery and public-page crawling.
It does not upsert a found supplier into CRM companies and never lets AI invent
an URL, price, contact or supplier name.
"""
from __future__ import annotations

import hashlib
import json
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from types import SimpleNamespace
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import EmailDelivery, GoogleSheetsConfig, MailAccount, ProductSearchDelivery, ProductSearchResult, ProductSearchRun, ProductSearchSheetConfig, Suppression
from app.services.company_enrichment import crawl_website
from app.services.deepseek import DeepSeekClient, DeepSeekError
from app.services.google_sheets import active_sheets_config, worksheet_from_config
from app.services.mailing import now_utc, safe_mail_error, smtp_connection
from app.services.recipients import normalize_email, valid_email
from app.services.secrets import decrypt_secret
from app.sources.phrase_search.service import EMAIL_RE, PHONE_RE, FreeSearchProvider, _organization_metadata, _page_evidence

PRICE_RE = re.compile(r"(?:от\s*)?(\d[\d\s]{0,12}(?:[,.]\d{1,2})?\s*(?:₽|руб(?:\.|лей)?))", re.I)
SHEET_HEADERS = (
    "Выбрать/снять", "Название компании", "Область", "Город", "Сайт", "Email", "Телефон",
    "Товар", "Цена", "Страница товара", "Статус", "LeadFlow ID",
)


class ProductDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relevant: bool
    company_name: str | None = None
    product_name: str | None = None
    price: str | None = None
    evidence_text: str | None = Field(default=None, max_length=500)


DECISION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["relevant", "company_name", "product_name", "price", "evidence_text"],
    "properties": {
        "relevant": {"type": "boolean"}, "company_name": {"type": ["string", "null"]},
        "product_name": {"type": ["string", "null"]}, "price": {"type": ["string", "null"]},
        "evidence_text": {"type": ["string", "null"], "maxLength": 500},
    },
}


def product_sheet_config(session: Session) -> ProductSearchSheetConfig:
    row = session.get(ProductSearchSheetConfig, "default")
    if row is None:
        row = ProductSearchSheetConfig(id="default")
        session.add(row)
        session.commit()
    return row


def _price(text: str) -> str | None:
    match = PRICE_RE.search(text)
    return re.sub(r"\s+", " ", match.group(0)).strip() if match else None


def _ai_decision(session: Session, settings: Settings, run: ProductSearchRun, url: str, text: str) -> ProductDecision:
    compact = text[:5500]
    payload = json.dumps({"query": run.query, "city": run.city, "region": run.region, "source_url": url, "page_text": compact}, ensure_ascii=False, separators=(",", ":"))
    result = DeepSeekClient(session, settings).generate(
        company_id=None, operation="product_search", content_hash=hashlib.sha256((url + "\n" + compact).encode()).hexdigest(),
        missing_fields=["relevance", "company_name", "product_name", "price"], prompt_version="product-search-v1",
        instructions=("Определи только по переданной публичной странице, есть ли предложение товара по запросу. "
                      "Не придумывай поставщика, цену, контакты, URL или характеристики. evidence_text должен быть "
                      "точной короткой цитатой из page_text; если доказательства нет, relevant=false и значения null."),
        input_text=payload, response_model=ProductDecision, schema=DECISION_SCHEMA, max_output_tokens=180, use_cache=True,
    )
    decision = result.data
    if not decision.evidence_text or decision.evidence_text.casefold() not in text.casefold():
        decision.relevant = False
        decision.company_name = decision.product_name = decision.price = None
    return decision


def execute_product_search(session: Session, run: ProductSearchRun, settings: Settings) -> ProductSearchRun:
    run.status = "running"
    session.commit()
    try:
        query = " ".join(part for part in (run.query, run.city, run.region) if part)
        urls = FreeSearchProvider().search(query, run.limit)
        run.urls_discovered = len(urls)
        session.commit()
        seen_sites: set[str] = set()
        for url in urls:
            try:
                snapshot = crawl_website(url, max_pages=2)
                if not snapshot.pages:
                    raise ValueError("Публичную страницу не удалось прочитать.")
                page = snapshot.pages[0]
                metadata = _organization_metadata(page.html)
                evidence_text = _page_evidence(page.text, run.query)
                price = _price(page.text)
                method, decision = "deterministic", None
                if run.use_ai and not evidence_text:
                    decision = _ai_decision(session, settings, run, page.url, page.text)
                    method, evidence_text = "ai", decision.evidence_text
                if not evidence_text or (decision and not decision.relevant):
                    session.add(ProductSearchResult(run_id=run.id, source_url=page.url, extraction_method=method, status="irrelevant", evidence=[]))
                    session.commit()
                    continue
                host = (urlsplit(page.url).hostname or "").removeprefix("www.")
                website = metadata.get("website") or f"{urlsplit(page.url).scheme}://{host}"
                # Never make a second supplier row solely because a search engine
                # returned another product page from the same website in one run.
                site_key = website.casefold().rstrip("/")
                if site_key in seen_sites:
                    run.duplicate_count += 1
                    session.add(ProductSearchResult(run_id=run.id, source_url=page.url, website=website, extraction_method=method, status="duplicate", evidence=[]))
                    session.commit()
                    continue
                seen_sites.add(site_key)
                emails, phones = EMAIL_RE.findall(page.text), PHONE_RE.findall(page.text)
                name = metadata.get("name") or (decision.company_name if decision else None) or host
                product = (decision.product_name if decision else None) or run.query
                price = price or (decision.price if decision else None)
                evidence = [{"field": "relevance", "value": run.query, "source_url": page.url, "evidence_text": evidence_text}]
                if price:
                    evidence.append({"field": "price", "value": price, "source_url": page.url, "evidence_text": price})
                session.add(ProductSearchResult(
                    run_id=run.id, source_url=page.url, product_url=page.url, company_name=name[:500],
                    region=run.region, city=metadata.get("city") or run.city, website=website,
                    email=metadata.get("email") or (emails[0] if emails else None),
                    phone=metadata.get("phone") or (phones[0] if phones else None), product_name=product[:1000],
                    price=price, extraction_method=method, status="found", evidence=evidence,
                ))
                run.result_count += 1
                session.commit()
            except Exception:
                session.rollback()
                session.add(ProductSearchResult(run_id=run.id, source_url=url, status="error", error="Страницу не удалось обработать."))
                session.commit()
        run = session.get(ProductSearchRun, run.id)
        run.status = "completed"
        sync_product_results(session, settings)
    except Exception as exc:
        session.rollback()
        run = session.get(ProductSearchRun, run.id)
        run.status = "failed"
        run.error = str(exc) if re.search(r"[А-Яа-яЁё]", str(exc)) else "Бесплатный поиск временно недоступен."
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run


def _worksheet(session: Session, settings: Settings):
    target = product_sheet_config(session)
    if not target.active or not target.spreadsheet_id:
        return None
    credentials = active_sheets_config(session, settings)
    if not credentials:
        raise ValueError("Google Таблица не подключена.")
    # ``worksheet_from_config`` only reads these three attributes, which keeps
    # the supplier table on the same service account without copying its secret.
    proxy = SimpleNamespace(credentials_encrypted=credentials.credentials_encrypted, spreadsheet_id=target.spreadsheet_id)
    return worksheet_from_config(proxy, target.worksheet_name, create_if_missing=True)


def sync_product_results(session: Session, settings: Settings) -> dict[str, int]:
    worksheet = _worksheet(session, settings)
    if worksheet is None:
        return {"inserted": 0, "updated": 0}
    rows = worksheet.get_all_values()
    if not rows:
        worksheet.update("A1:L1", [list(SHEET_HEADERS)])
        rows = [list(SHEET_HEADERS)]
    header = rows[0]
    if tuple(header[:len(SHEET_HEADERS)]) != SHEET_HEADERS:
        # Explicitly reject an accidental CRM tab instead of silently moving
        # columns in a user-maintained worksheet.
        raise ValueError("В таблице поставщиков не найдены ожидаемые заголовки.")
    id_index = len(SHEET_HEADERS) - 1
    by_id = {row[id_index]: number for number, row in enumerate(rows[1:], start=2) if len(row) > id_index and row[id_index]}
    inserted = updated = 0
    results = list(session.scalars(select(ProductSearchResult).order_by(ProductSearchResult.created_at)))
    # Only usable supplier records belong in the working sheet.  Discovery
    # diagnostics (irrelevant pages, duplicates and fetch errors) remain in the
    # run history/API, but must not create empty rows for the user.
    visible_statuses = {"found", "sent"}
    # Rebuild the data area: this dedicated worksheet is a projection of the
    # current run results, so stale diagnostic rows must disappear rather than
    # remain as blank/error records. Preserve the user's checkbox selection by
    # stable result ID.
    selected_by_id = {row[id_index]: row[0] for row in rows[1:] if len(row) > id_index and row[id_index]}
    worksheet.batch_clear([f"A2:L{max(2, len(rows) + 1)}"])
    for item in results:
        item.selected = selected_by_id.get(item.id, "FALSE") == "TRUE"
        item.sheet_row = None
    rows = [list(SHEET_HEADERS)]
    by_id = {}
    for item in results:
        if item.status not in visible_statuses:
            continue
        value = [
            "TRUE" if item.selected else "FALSE", item.company_name or "", item.region or "", item.city or "", item.website or "",
            item.email or "", item.phone or "", item.product_name or "", item.price or "", item.product_url or "",
            {"found": "Найдено", "duplicate": "Дубль", "irrelevant": "Нерелевантно", "error": "Ошибка", "sent": "Отправлено"}.get(item.status, item.status), item.id,
        ]
        row_number = by_id.get(item.id)
        if row_number:
            # RAW prevents phone numbers such as +7... from being interpreted
            # as spreadsheet formulas (#ERROR!).
            worksheet.update(f"A{row_number}:L{row_number}", [value], value_input_option="RAW")
            updated += 1
        else:
            worksheet.append_row(value, value_input_option="RAW")
            row_number = len(rows) + inserted + 1
            inserted += 1
        item.sheet_row = row_number
    # Google Sheets checkboxes are an affordance, not an identifier.  The
    # stable result ID stays in a hidden last column.
    try:
        worksheet.add_validation(f"A2:A1000", "boolean", ["TRUE", "FALSE"], strict=True)
        worksheet.spreadsheet.batch_update({"requests": [{"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": id_index, "endIndex": id_index + 1}, "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}]})
    except Exception:
        pass
    session.commit()
    return {"inserted": inserted, "updated": updated}


def read_product_sheet_selection(session: Session, settings: Settings) -> int:
    worksheet = _worksheet(session, settings)
    if worksheet is None:
        return 0
    rows = worksheet.get_all_values()
    changed = 0
    for row in rows[1:]:
        if len(row) < len(SHEET_HEADERS) or not row[-1]:
            continue
        item = session.get(ProductSearchResult, row[-1])
        if item is None:
            continue
        selected = row[0].strip().casefold() in {"true", "1", "да", "✓"}
        if item.selected != selected:
            item.selected = selected
            changed += 1
    session.commit()
    return changed


def serialize_product_result(item: ProductSearchResult) -> dict[str, object]:
    return {key: getattr(item, key) for key in (
        "id", "run_id", "source_url", "product_url", "company_name", "region", "city", "website", "email", "phone",
        "product_name", "price", "extraction_method", "status", "selected", "evidence", "error", "sheet_row", "created_at",
    )}


def _render_supplier_text(value: str, item: ProductSearchResult, account: MailAccount) -> str:
    replacements = {
        "company_name": item.company_name or "", "product_name": item.product_name or "",
        "price": item.price or "", "city": item.city or "", "sender_name": account.from_name or account.name,
        "sender_email": account.from_email,
    }
    for key, replacement in replacements.items():
        value = value.replace("{{" + key + "}}", replacement)
    return value


def send_selected_supplier_requests(
    session: Session, settings: Settings, *, mailbox_id: str, subject: str, text_body: str, result_ids: list[str] | None = None,
) -> dict[str, int]:
    """Send only explicitly selected supplier enquiries after all safeguards.

    This is deliberately synchronous: it is an administrator-confirmed action,
    not an automatic campaign.  A unique result/email pair makes repeated
    clicks safe.
    """
    account = session.get(MailAccount, mailbox_id)
    if not account or not account.active:
        raise ValueError("Почтовый ящик недоступен.")
    query = select(ProductSearchResult)
    if result_ids:
        query = query.where(ProductSearchResult.id.in_(result_ids))
    else:
        query = query.where(ProductSearchResult.selected.is_(True))
    items = list(session.scalars(query))
    if not items:
        raise ValueError("Не выбраны компании для отправки.")
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    used = (session.scalar(select(func.count()).select_from(EmailDelivery).where(EmailDelivery.mailbox_id == account.id, EmailDelivery.sent_at >= today)) or 0) + (session.scalar(select(func.count()).select_from(ProductSearchDelivery).where(ProductSearchDelivery.mailbox_id == account.id, ProductSearchDelivery.sent_at >= today)) or 0)
    sent = skipped = errors = 0
    for item in items:
        recipient = valid_email(item.email)
        if not recipient or session.get(Suppression, normalize_email(recipient)):
            skipped += 1
            continue
        if used >= account.daily_limit:
            errors += 1
            break
        existing = session.scalar(select(ProductSearchDelivery).where(ProductSearchDelivery.result_id == item.id, ProductSearchDelivery.recipient_email == recipient))
        if existing and existing.status == "sent":
            skipped += 1
            continue
        delivery = existing or ProductSearchDelivery(result_id=item.id, mailbox_id=account.id, recipient_email=recipient, subject="", text_body="")
        delivery.subject = _render_supplier_text(subject, item, account)
        delivery.text_body = _render_supplier_text(text_body, item, account)
        delivery.status, delivery.error = "sending", None
        delivery.message_id = delivery.message_id or make_msgid(domain=account.from_email.split("@")[-1])
        session.add(delivery); session.commit()
        message = EmailMessage()
        message["Subject"] = delivery.subject
        message["From"] = formataddr((account.from_name or account.name, account.from_email))
        message["To"] = recipient
        message["Message-ID"] = delivery.message_id
        message.set_content(delivery.text_body)
        try:
            with smtp_connection(account) as smtp:
                smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted))
                refused = smtp.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            delivery.status, delivery.sent_at, item.status = "sent", now_utc(), "sent"
            sent += 1; used += 1
        except Exception as exc:
            delivery.status, delivery.error = "send_error", safe_mail_error(exc)
            errors += 1
        session.commit()
    try:
        sync_product_results(session, settings)
    except Exception:
        session.rollback()
    return {"sent": sent, "skipped": skipped, "errors": errors}
