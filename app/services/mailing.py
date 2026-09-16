from __future__ import annotations

import imaplib
import re
import secrets
import smtplib
import socket
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from html import escape
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from jinja2 import Environment, StrictUndefined, select_autoescape
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings, is_branded_https_url, is_public_https_url
from app.models import Campaign, Company, CompanyDirection, Direction, EmailDelivery, EmailTemplate, MailAccount, SenderSettings, Suppression, TrackedLink
from app.services.secrets import decrypt_secret
from app.services.email_events import record_email_event, sync_email_event_to_sheets
from app.services.attachments import active_attachments, attachment_path

HREF_RE = re.compile(r'(<a\b[^>]*?\bhref=["\'])(https?://[^"\']+)(["\'])', re.I)
INLINE_LOGO_RE = re.compile(r'https?://[^"\']+/email-assets/bogorodsky-pryanik-logo\.jpg', re.I)
INLINE_LOGO_CID = "bogorodsky-pryanik-logo"
template_env = Environment(autoescape=select_autoescape(["html", "xml"]), undefined=StrictUndefined)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def safe_mail_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError): return "Не удалось войти в почту. Проверьте адрес и пароль приложения."
    if isinstance(exc, imaplib.IMAP4.error): return "Не удалось проверить входящие письма."
    if isinstance(exc, (socket.timeout, TimeoutError)): return "Почтовый сервер не ответил вовремя. Попробуйте ещё раз."
    if isinstance(exc, (ConnectionError, OSError)): return "Не удалось подключиться к почте. Проверьте интернет и настройки."
    if isinstance(exc, smtplib.SMTPException): return "Не удалось отправить письмо. Система попробует ещё раз."
    return "Не удалось выполнить операцию с почтой."


def render_template_parts(template: EmailTemplate, company: Company, extra: dict[str, str] | None = None, account: MailAccount | None = None) -> tuple[str, str, str]:
    context: dict[str, object] = {
        "company_name": company.company_name, "city": company.city or "", "region": company.region or "",
        "category": company.category or "", "website": company.website or "",
        "decision_maker_name": company.decision_maker_name or "",
        "decision_maker_position": company.decision_maker_position or "",
        "sender_name": (account.from_name or account.name) if account else "",
        "sender_position": "", "sender_phone": "", "sender_email": account.from_email if account else "",
        "sender_site": "", "sender_company": "",
    }
    context.update(extra or {})
    return (
        template_env.from_string(template.subject_template).render(context),
        template_env.from_string(template.html_template).render(context),
        template_env.from_string(template.text_template or "").render(context),
    )


def sender_template_context(session: Session, account: MailAccount | None = None) -> dict[str, str]:
    row = session.get(SenderSettings, "default")
    return {
        "sender_name": (row.display_name if row else None) or ((account.from_name or account.name) if account else ""),
        "sender_position": (row.position if row else None) or "",
        "sender_phone": (row.phone if row else None) or "",
        "sender_email": (row.email if row else None) or (account.from_email if account else ""),
        "sender_site": (row.website if row else None) or "",
        "sender_company": (row.company_name if row else None) or "",
    }


def render_template(template: EmailTemplate, company: Company, extra: dict[str, str] | None = None, account: MailAccount | None = None) -> tuple[str, str]:
    subject, html, _ = render_template_parts(template, company, extra, account)
    return subject, html


def _is_test_recipient(value: str) -> bool:
    domain = value.rsplit("@", 1)[-1].casefold()
    return domain == "example.test" or domain.endswith(".test")


def _unsubscribe_url(delivery: EmailDelivery, settings: Settings, account: MailAccount) -> str:
    if is_branded_https_url(settings.public_base_url):
        return urljoin(settings.public_base_url.rstrip("/") + "/", f"unsubscribe/{delivery.unsubscribe_token}")
    subject = quote("Отписаться от писем", safe="")
    return f"mailto:{account.from_email}?subject={subject}"


def add_tracking(session: Session, delivery: EmailDelivery, html: str, settings: Settings, account: MailAccount) -> str:
    def replace(match: re.Match[str]) -> str:
        token = secrets.token_urlsafe(32)
        session.add(TrackedLink(token=token, delivery_id=delivery.id, target_url=match.group(2)))
        return f'{match.group(1)}{urljoin(settings.public_base_url.rstrip("/") + "/", f"t/c/{token}")}{match.group(3)}'
    branded_origin = is_branded_https_url(settings.public_base_url)
    if settings.email_click_tracking_enabled and branded_origin:
        html = HREF_RE.sub(replace, html)
    if settings.email_open_tracking_enabled and branded_origin:
        pixel = urljoin(settings.public_base_url.rstrip("/") + "/", f"t/open/{delivery.tracking_token}.gif")
        html += f'<img src="{escape(pixel)}" width="1" height="1" alt="" style="display:none">'
    delivery.unsubscribe_url = _unsubscribe_url(delivery, settings, account)
    return html + (
        f'<p style="font-size:12px;color:#777">'
        f'<a href="{escape(delivery.unsubscribe_url)}" rel="nofollow">Отписаться от рассылки</a></p>'
    )


def add_text_unsubscribe(text: str, unsubscribe_url: str) -> str:
    clean = text.rstrip()
    suffix = f"Чтобы больше не получать письма, отпишитесь: {unsubscribe_url}"
    return f"{clean}\n\n{suffix}" if clean else suffix


def delivery_preflight_errors(delivery: EmailDelivery, account: MailAccount) -> list[str]:
    errors: list[str] = []
    if not delivery.subject.strip():
        errors.append("Пустая тема письма")
    if not delivery.text_body.strip():
        errors.append("Отсутствует текстовая версия письма")
    if not delivery.unsubscribe_url:
        errors.append("Отсутствует ссылка отписки")
    elif not (delivery.unsubscribe_url.startswith("mailto:") or is_public_https_url(delivery.unsubscribe_url)):
        errors.append("Ссылка отписки должна вести на почту или публичный HTTPS-адрес")
    combined = f"{delivery.html_body}\n{delivery.text_body}".casefold()
    if not _is_test_recipient(delivery.recipient_email) and any(
        marker in combined for marker in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")
    ):
        errors.append("Письмо содержит локальный или недоступный адрес")
    if not account.from_name or "test" in account.from_name.casefold():
        if not _is_test_recipient(delivery.recipient_email):
            errors.append("Укажите реальное имя отправителя вместо тестового")
    return errors


def smtp_connection(account: MailAccount):
    if account.smtp_security == "ssl": return smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=20)
    smtp = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=20)
    if account.smtp_security == "starttls": smtp.starttls()
    return smtp


def imap_connection(account: MailAccount):
    if account.imap_security == "ssl": return imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=20)
    client = imaplib.IMAP4(account.imap_host, account.imap_port, timeout=20)
    if account.imap_security == "starttls": client.starttls()
    return client


def build_email_message(delivery: EmailDelivery, account: MailAccount) -> EmailMessage:
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = delivery.subject, formataddr((account.from_name or account.name, account.from_email)), delivery.recipient_email
    message["Date"] = format_datetime(now_utc())
    if account.reply_to: message["Reply-To"] = account.reply_to
    message["Message-ID"] = delivery.message_id
    if delivery.unsubscribe_url:
        message["List-Unsubscribe"] = f"<{delivery.unsubscribe_url}>"
        if urlparse(delivery.unsubscribe_url).scheme == "https":
            message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if delivery.in_reply_to:
        message["In-Reply-To"] = delivery.in_reply_to
        message["References"] = delivery.in_reply_to
    message.set_content(delivery.text_body or "Это письмо содержит HTML-версию.")
    html_body, replacements = INLINE_LOGO_RE.subn(f"cid:{INLINE_LOGO_CID}", delivery.html_body)
    message.add_alternative(html_body, subtype="html")
    logo_path = Path(__file__).resolve().parents[2] / "logo.jpg"
    if replacements and logo_path.is_file():
        message.get_payload()[-1].add_related(
            logo_path.read_bytes(), maintype="image", subtype="jpeg",
            cid=f"<{INLINE_LOGO_CID}>", filename="bogorodsky-pryanik-logo.jpg", disposition="inline",
        )
    settings = get_settings()
    for item in delivery.attachments_snapshot or []:
        path = attachment_path(settings, str(item.get("storage_name") or ""))
        if not path.is_file():
            raise FileNotFoundError(f"Не найден файл вложения: {item.get('filename') or 'файл'}")
        content_type = str(item.get("content_type") or "application/octet-stream")
        maintype, subtype = content_type.split("/", 1) if "/" in content_type else ("application", "octet-stream")
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=str(item.get("filename") or path.name))
    return message


def diagnose_smtp(account: MailAccount) -> dict[str, bool | str]:
    result: dict[str, bool | str] = {"connection": False, "authentication": False}
    try:
        with smtp_connection(account) as smtp:
            result["connection"] = True
            smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted))
            result["authentication"] = True
    except Exception as exc: result["error"] = safe_mail_error(exc)
    return result


def diagnose_imap(account: MailAccount) -> dict[str, bool | str]:
    result: dict[str, bool | str] = {"connection": False, "authentication": False}
    try:
        with imap_connection(account) as client:
            result["connection"] = True
            client.login(account.imap_login, decrypt_secret(account.imap_password_encrypted))
            result["authentication"] = True
    except Exception as exc: result["error"] = safe_mail_error(exc)
    return result


def test_smtp(account: MailAccount) -> None:
    result = diagnose_smtp(account)
    if not result["authentication"]: raise ConnectionError(str(result.get("error", "SMTP test failed")))


def test_imap(account: MailAccount) -> None:
    result = diagnose_imap(account)
    if not result["authentication"]: raise ConnectionError(str(result.get("error", "IMAP test failed")))


def is_suppressed(session: Session, recipient: str) -> bool:
    return bool(session.scalar(select(Suppression.email).where(Suppression.email == normalize_email(recipient), Suppression.active.is_(True))))


def mailbox_daily_limit_reached(session: Session, account: MailAccount) -> bool:
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    used = session.scalar(select(func.count()).select_from(EmailDelivery).where(
        EmailDelivery.mailbox_id == account.id, EmailDelivery.sent_at >= today,
    )) or 0
    pending = session.scalar(select(func.count()).select_from(EmailDelivery).where(
        EmailDelivery.mailbox_id == account.id, EmailDelivery.status.in_(["queued", "sending"]),
    )) or 0
    return used + pending >= account.daily_limit


def build_delivery(session: Session, company: Company, account: MailAccount, template: EmailTemplate, settings: Settings, *, campaign: Campaign | None = None, direction_id: str | None = None, send_mode: str = "manual", overrides: dict[str, str | None] | None = None, extra: dict[str, str] | None = None, recipient_override: str | None = None, idempotency_key: str | None = None, attachments_snapshot: list[dict[str, object]] | None = None) -> EmailDelivery:
    if idempotency_key:
        existing = session.scalar(select(EmailDelivery).where(EmailDelivery.idempotency_key == idempotency_key))
        if existing:
            return existing
    recipient = normalize_email(recipient_override or company.decision_maker_email or company.company_email or company.email or "")
    if not recipient: raise ValueError("Company has no recipient email")
    if is_suppressed(session, recipient) or company.manually_blocked: raise ValueError("Recipient is suppressed")
    if mailbox_daily_limit_reached(session, account): raise ValueError("Mailbox daily limit reached")
    if campaign and session.scalar(select(EmailDelivery.id).where(EmailDelivery.campaign_id == campaign.id, EmailDelivery.company_id == company.id, EmailDelivery.recipient_email == recipient)):
        raise ValueError("This campaign was already sent or queued for the recipient")
    if campaign and campaign.cooldown_days:
        cutoff = now_utc() - timedelta(days=campaign.cooldown_days)
        if session.scalar(select(EmailDelivery.id).where(EmailDelivery.recipient_email == recipient, EmailDelivery.sent_at >= cutoff)):
            raise ValueError("Recipient is in the configured cooldown period")
    merged_extra = sender_template_context(session, account)
    merged_extra.update(extra or {})
    subject, html, text = render_template_parts(template, company, extra=merged_extra, account=account)
    overrides = overrides or {}
    subject, html = overrides.get("subject") or subject, overrides.get("html_body") or html
    text = overrides.get("text_body") if overrides.get("text_body") is not None else text
    resolved_direction_id = direction_id or (campaign.direction_id if campaign else template.direction_id)
    delivery = EmailDelivery(
        campaign_id=campaign.id if campaign else None, direction_id=resolved_direction_id,
        company_id=company.id, mailbox_id=account.id, template_id=template.id, recipient_email=recipient,
        recipient_name=company.decision_maker_name, subject=subject, html_body="", text_body=text or "", send_mode=send_mode,
        status="queued", tracking_token=secrets.token_urlsafe(32), unsubscribe_token=secrets.token_urlsafe(32), next_attempt_at=now_utc(),
        idempotency_key=idempotency_key,
        attachments_snapshot=attachments_snapshot if attachments_snapshot is not None else (
            active_attachments(session, resolved_direction_id) if resolved_direction_id else []
        ),
    )
    session.add(delivery); session.flush()
    delivery.html_body = add_tracking(session, delivery, html, settings, account)
    delivery.text_body = add_text_unsubscribe(delivery.text_body, delivery.unsubscribe_url or "")
    record_email_event(session, delivery, "queued", {"send_mode": send_mode})
    session.commit(); session.refresh(delivery)
    return delivery


def queue_campaign(session: Session, campaign: Campaign, settings: Settings) -> dict[str, int]:
    account, template = session.get(MailAccount, campaign.mailbox_id), session.get(EmailTemplate, campaign.template_id)
    if campaign.direction_id and (not template or template.direction_id != campaign.direction_id):
        direction = session.get(Direction, campaign.direction_id)
        template = session.get(EmailTemplate, direction.automatic_template_id) if direction and direction.automatic_template_id else None
        template = template or session.scalar(select(EmailTemplate).where(
            EmailTemplate.direction_id == campaign.direction_id, EmailTemplate.active.is_(True),
        ).order_by(EmailTemplate.created_at).limit(1))
    if not account or not account.active or not template or not template.active: raise ValueError("Campaign mailbox or template is inactive")
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    mailbox_used = session.scalar(select(func.count()).select_from(EmailDelivery).where(
        EmailDelivery.mailbox_id == account.id, EmailDelivery.sent_at >= today,
    )) or 0
    mailbox_pending = session.scalar(select(func.count()).select_from(EmailDelivery).where(
        EmailDelivery.mailbox_id == account.id, EmailDelivery.status.in_(["queued", "sending"]),
    )) or 0
    campaign_used = session.scalar(select(func.count()).select_from(EmailDelivery).where(
        EmailDelivery.campaign_id == campaign.id, EmailDelivery.sent_at >= today,
    )) or 0
    campaign_pending = session.scalar(select(func.count()).select_from(EmailDelivery).where(
        EmailDelivery.campaign_id == campaign.id, EmailDelivery.status.in_(["queued", "sending"]),
    )) or 0
    mailbox_remaining = max(0, account.daily_limit - mailbox_used - mailbox_pending)
    campaign_remaining = max(0, campaign.daily_limit - campaign_used - campaign_pending)
    limit = min(mailbox_remaining, campaign_remaining, campaign.run_limit)
    if not limit: return {"selected": 0, "queued": 0, "skipped": 0}
    email_value = func.coalesce(Company.decision_maker_email, Company.company_email, Company.email)
    query = select(Company).where(email_value.is_not(None), Company.manually_blocked.is_(False))
    if campaign.direction_id: query = query.join(CompanyDirection).where(CompanyDirection.direction_id == campaign.direction_id)
    elif campaign.category: query = query.where(Company.category == campaign.category)
    if campaign.city: query = query.where(Company.city == campaign.city)
    companies = list(session.scalars(query.order_by(Company.collected_at).limit(limit * 20)))
    queued = skipped = 0
    for company in companies:
        if queued >= limit: break
        try:
            delivery = build_delivery(session, company, account, template, settings, campaign=campaign, send_mode="campaign")
            delivery.next_attempt_at = now_utc() + timedelta(seconds=campaign.sending_interval_seconds * queued)
            session.commit(); queued += 1
        except (ValueError, IntegrityError):
            session.rollback(); skipped += 1
    return {"selected": len(companies), "queued": queued, "skipped": skipped}


def execute_campaign(session: Session, campaign: Campaign, settings: Settings) -> dict[str, int]:
    campaign.last_run_at = now_utc()
    try:
        result = queue_campaign(session, campaign, settings)
        campaign.last_queued = result["queued"]
        campaign.last_error = None
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        campaign = session.get(Campaign, campaign.id)
        campaign.last_run_at = now_utc()
        campaign.last_queued = 0
        campaign.last_error = safe_mail_error(exc) if not isinstance(exc, ValueError) else str(exc)
        session.commit()
        raise


def claim_delivery(session: Session, worker_id: str) -> str | None:
    now, stale = now_utc(), now_utc() - timedelta(minutes=10)
    delivery = session.scalar(select(EmailDelivery).where(
        EmailDelivery.attempt_count < EmailDelivery.max_attempts,
        or_(and_(EmailDelivery.status.in_(["queued", "send_error"]), or_(EmailDelivery.next_attempt_at.is_(None), EmailDelivery.next_attempt_at <= now)), and_(EmailDelivery.status == "sending", EmailDelivery.locked_at < stale)),
    ).order_by(EmailDelivery.created_at).with_for_update(skip_locked=True).limit(1))
    if not delivery: return None
    delivery.status, delivery.locked_at, delivery.locked_by = "sending", now, worker_id
    delivery.attempt_count += 1
    account = session.get(MailAccount, delivery.mailbox_id)
    delivery.message_id = delivery.message_id or make_msgid(domain=(account.from_email.split("@")[-1] if account else None))
    record_email_event(session, delivery, "sending", {"attempt": delivery.attempt_count})
    session.commit()
    return delivery.id


def send_claimed_delivery(session: Session, delivery_id: str, worker_id: str) -> str:
    delivery = session.get(EmailDelivery, delivery_id)
    if not delivery or delivery.status != "sending" or delivery.locked_by != worker_id: return "lost_lock"
    account = session.get(MailAccount, delivery.mailbox_id)
    if not account or not account.active or is_suppressed(session, delivery.recipient_email):
        delivery.status, delivery.error = "send_error", "Mailbox inactive or recipient suppressed"
        delivery.next_attempt_at = now_utc() + timedelta(minutes=2 ** min(delivery.attempt_count, 6))
        record_email_event(session, delivery, "send_error", {"attempt": delivery.attempt_count})
    else:
        message = build_email_message(delivery, account)
        preflight_errors = delivery_preflight_errors(delivery, account)
        if preflight_errors:
            delivery.status = "send_error"
            delivery.error = "; ".join(preflight_errors)
            delivery.attempt_count = delivery.max_attempts
            delivery.next_attempt_at = None
            delivery.locked_at = delivery.locked_by = None
            record_email_event(session, delivery, "send_error", {"reason": "deliverability_preflight"})
            session.commit()
            sync_email_event_to_sheets(session, delivery)
            return delivery.status
        try:
            with smtp_connection(account) as smtp:
                smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted)); refused = smtp.send_message(message)
            if refused: raise smtplib.SMTPRecipientsRefused(refused)
            delivery.status, delivery.sent_at, delivery.provider_message_id, delivery.error = "sent", now_utc(), delivery.message_id, None
            record_email_event(session, delivery, "sent")
            company = session.get(Company, delivery.company_id)
            if company:
                company.communication_started_at = company.communication_started_at or delivery.sent_at
                company.action = "Отправлено предложение"
        except Exception as exc:
            delivery.error = safe_mail_error(exc)
            code = getattr(exc, "smtp_code", None)
            if (isinstance(code, int) and code >= 500) or isinstance(exc, smtplib.SMTPRecipientsRefused):
                delivery.status, delivery.bounced_at = "bounced", now_utc()
                record_email_event(session, delivery, "bounced", {"reason": "hard_bounce"})
                session.merge(Suppression(email=normalize_email(delivery.recipient_email), reason="hard_bounce", source_delivery_id=delivery.id, active=True))
            else:
                delivery.status = "send_error"; delivery.next_attempt_at = now_utc() + timedelta(minutes=2 ** min(delivery.attempt_count, 6))
                record_email_event(session, delivery, "send_error", {"attempt": delivery.attempt_count})
    delivery.locked_at = delivery.locked_by = None
    session.commit()
    sync_email_event_to_sheets(session, delivery)
    return delivery.status


def process_queue(session: Session, settings: Settings, worker_id: str, limit: int = 10) -> dict[str, int]:
    result = {"sent": 0, "errors": 0}
    for _ in range(limit):
        delivery_id = claim_delivery(session, worker_id)
        if not delivery_id: break
        result["sent" if send_claimed_delivery(session, delivery_id, worker_id) == "sent" else "errors"] += 1
    return result


def send_delivery(session: Session, delivery: EmailDelivery, account: MailAccount) -> None:
    worker_id = f"sync-{secrets.token_hex(6)}"
    delivery.status, delivery.locked_by, delivery.locked_at = "sending", worker_id, now_utc()
    delivery.attempt_count += 1
    delivery.message_id = delivery.message_id or make_msgid(domain=account.from_email.split("@")[-1])
    record_email_event(session, delivery, "sending", {"attempt": delivery.attempt_count})
    session.commit(); send_claimed_delivery(session, delivery.id, worker_id)
