from __future__ import annotations

import imaplib
import re
import secrets
import smtplib
import socket
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from urllib.parse import urljoin

from jinja2 import Environment, StrictUndefined, select_autoescape
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Campaign, Company, CompanyDirection, EmailDelivery, EmailTemplate, MailAccount, Suppression, TrackedLink
from app.services.secrets import decrypt_secret

HREF_RE = re.compile(r'(<a\b[^>]*?\bhref=["\'])(https?://[^"\']+)(["\'])', re.I)
template_env = Environment(autoescape=select_autoescape(["html", "xml"]), undefined=StrictUndefined)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def safe_mail_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError): return "SMTP authentication failed"
    if isinstance(exc, imaplib.IMAP4.error): return "IMAP authentication or protocol error"
    if isinstance(exc, (socket.timeout, TimeoutError)): return "Mail server connection timed out"
    if isinstance(exc, (ConnectionError, OSError)): return f"Mail server connection failed ({exc.__class__.__name__})"
    if isinstance(exc, smtplib.SMTPException): return f"SMTP operation failed ({exc.__class__.__name__})"
    return f"Mail operation failed ({exc.__class__.__name__})"


def render_template_parts(template: EmailTemplate, company: Company, extra: dict[str, str] | None = None, account: MailAccount | None = None) -> tuple[str, str, str]:
    context: dict[str, object] = {
        "company_name": company.company_name, "city": company.city or "", "region": company.region or "",
        "category": company.category or "", "website": company.website or "",
        "decision_maker_name": company.decision_maker_name or "",
        "decision_maker_position": company.decision_maker_position or "",
        "sender_name": (account.from_name or account.name) if account else "",
    }
    context.update(extra or {})
    return (
        template_env.from_string(template.subject_template).render(context),
        template_env.from_string(template.html_template).render(context),
        template_env.from_string(template.text_template or "").render(context),
    )


def render_template(template: EmailTemplate, company: Company, extra: dict[str, str] | None = None, account: MailAccount | None = None) -> tuple[str, str]:
    subject, html, _ = render_template_parts(template, company, extra, account)
    return subject, html


def add_tracking(session: Session, delivery: EmailDelivery, html: str, settings: Settings) -> str:
    def replace(match: re.Match[str]) -> str:
        token = secrets.token_urlsafe(32)
        session.add(TrackedLink(token=token, delivery_id=delivery.id, target_url=match.group(2)))
        return f'{match.group(1)}{urljoin(settings.public_base_url.rstrip("/") + "/", f"t/c/{token}")}{match.group(3)}'
    html = HREF_RE.sub(replace, html)
    pixel = urljoin(settings.public_base_url.rstrip("/") + "/", f"t/open/{delivery.tracking_token}.gif")
    unsubscribe = urljoin(settings.public_base_url.rstrip("/") + "/", f"unsubscribe/{delivery.unsubscribe_token}")
    return html + f'<img src="{escape(pixel)}" width="1" height="1" alt="" style="display:none">' + f'<p style="font-size:12px;color:#777"><a href="{escape(unsubscribe)}">Отписаться</a></p>'


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


def build_delivery(session: Session, company: Company, account: MailAccount, template: EmailTemplate, settings: Settings, *, campaign: Campaign | None = None, direction_id: str | None = None, send_mode: str = "manual", overrides: dict[str, str | None] | None = None, extra: dict[str, str] | None = None, recipient_override: str | None = None) -> EmailDelivery:
    recipient = normalize_email(recipient_override or company.decision_maker_email or company.company_email or company.email or "")
    if not recipient: raise ValueError("Company has no recipient email")
    if is_suppressed(session, recipient) or company.manually_blocked: raise ValueError("Recipient is suppressed")
    if campaign and session.scalar(select(EmailDelivery.id).where(EmailDelivery.campaign_id == campaign.id, EmailDelivery.company_id == company.id, EmailDelivery.recipient_email == recipient)):
        raise ValueError("This campaign was already sent or queued for the recipient")
    if campaign and campaign.cooldown_days:
        cutoff = now_utc() - timedelta(days=campaign.cooldown_days)
        if session.scalar(select(EmailDelivery.id).where(EmailDelivery.recipient_email == recipient, EmailDelivery.sent_at >= cutoff)):
            raise ValueError("Recipient is in the configured cooldown period")
    subject, html, text = render_template_parts(template, company, extra=extra, account=account)
    overrides = overrides or {}
    subject, html = overrides.get("subject") or subject, overrides.get("html_body") or html
    text = overrides.get("text_body") if overrides.get("text_body") is not None else text
    delivery = EmailDelivery(
        campaign_id=campaign.id if campaign else None, direction_id=direction_id or (campaign.direction_id if campaign else template.direction_id),
        company_id=company.id, mailbox_id=account.id, template_id=template.id, recipient_email=recipient,
        recipient_name=company.decision_maker_name, subject=subject, html_body="", text_body=text or "", send_mode=send_mode,
        status="queued", tracking_token=secrets.token_urlsafe(32), unsubscribe_token=secrets.token_urlsafe(32), next_attempt_at=now_utc(),
    )
    session.add(delivery); session.flush()
    delivery.html_body = add_tracking(session, delivery, html, settings)
    session.commit(); session.refresh(delivery)
    return delivery


def queue_campaign(session: Session, campaign: Campaign, settings: Settings) -> dict[str, int]:
    account, template = session.get(MailAccount, campaign.mailbox_id), session.get(EmailTemplate, campaign.template_id)
    if not account or not account.active or not template or not template.active: raise ValueError("Campaign mailbox or template is inactive")
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    used = session.scalar(select(func.count()).select_from(EmailDelivery).where(EmailDelivery.mailbox_id == account.id, EmailDelivery.sent_at >= today)) or 0
    pending = session.scalar(select(func.count()).select_from(EmailDelivery).where(EmailDelivery.mailbox_id == account.id, EmailDelivery.status.in_(["queued", "sending"]))) or 0
    limit = min(max(0, min(campaign.daily_limit, account.daily_limit) - used - pending), campaign.run_limit)
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
    return queue_campaign(session, campaign, settings)


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
    session.commit()
    return delivery.id


def send_claimed_delivery(session: Session, delivery_id: str, worker_id: str) -> str:
    delivery = session.get(EmailDelivery, delivery_id)
    if not delivery or delivery.status != "sending" or delivery.locked_by != worker_id: return "lost_lock"
    account = session.get(MailAccount, delivery.mailbox_id)
    if not account or not account.active or is_suppressed(session, delivery.recipient_email):
        delivery.status, delivery.error = "send_error", "Mailbox inactive or recipient suppressed"
    else:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = delivery.subject, formataddr((account.from_name or account.name, account.from_email)), delivery.recipient_email
        if account.reply_to: message["Reply-To"] = account.reply_to
        message["Message-ID"], message["X-LeadFlow-ID"] = delivery.message_id, delivery.id
        if delivery.in_reply_to:
            message["In-Reply-To"] = delivery.in_reply_to
            message["References"] = delivery.in_reply_to
        message.set_content(delivery.text_body or "Это письмо содержит HTML-версию."); message.add_alternative(delivery.html_body, subtype="html")
        try:
            with smtp_connection(account) as smtp:
                smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted)); refused = smtp.send_message(message)
            if refused: raise smtplib.SMTPRecipientsRefused(refused)
            delivery.status, delivery.sent_at, delivery.provider_message_id, delivery.error = "sent", now_utc(), delivery.message_id, None
            company = session.get(Company, delivery.company_id)
            if company:
                company.communication_started_at = company.communication_started_at or delivery.sent_at
                company.action = "Отправлено предложение"
        except Exception as exc:
            delivery.error = safe_mail_error(exc)
            code = getattr(exc, "smtp_code", None)
            if (isinstance(code, int) and code >= 500) or isinstance(exc, smtplib.SMTPRecipientsRefused):
                delivery.status, delivery.bounced_at = "bounced", now_utc()
                session.merge(Suppression(email=normalize_email(delivery.recipient_email), reason="hard_bounce", source_delivery_id=delivery.id, active=True))
            else:
                delivery.status = "send_error"; delivery.next_attempt_at = now_utc() + timedelta(minutes=2 ** min(delivery.attempt_count, 6))
    delivery.locked_at = delivery.locked_by = None
    session.commit(); return delivery.status


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
    session.commit(); send_claimed_delivery(session, delivery.id, worker_id)
