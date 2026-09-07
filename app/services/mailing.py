from __future__ import annotations

import re
import secrets
import smtplib
import imaplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from urllib.parse import urljoin

from jinja2 import Environment, StrictUndefined, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Campaign, Company, EmailDelivery, EmailTemplate, MailAccount, Suppression, TrackedLink
from app.services.secrets import decrypt_secret

HREF_RE = re.compile(r'(<a\b[^>]*?\bhref=["\'])(https?://[^"\']+)(["\'])', re.IGNORECASE)
template_env = Environment(autoescape=select_autoescape(["html", "xml"]), undefined=StrictUndefined)


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def render_template(template: EmailTemplate, company: Company, extra: dict[str, str] | None = None) -> tuple[str, str]:
    context = {
        "company_name": company.company_name,
        "city": company.city or "",
        "category": company.category or "",
        "website": company.website or "",
    }
    context.update(extra or {})
    subject = template_env.from_string(template.subject_template).render(context)
    html = template_env.from_string(template.html_template).render(context)
    return subject, html


def add_tracking(session: Session, delivery: EmailDelivery, html: str, settings: Settings) -> str:
    def replace(match: re.Match[str]) -> str:
        token = secrets.token_urlsafe(24)
        session.add(TrackedLink(token=token, delivery_id=delivery.id, target_url=match.group(2)))
        tracked = urljoin(settings.public_base_url.rstrip("/") + "/", f"t/c/{token}")
        return f"{match.group(1)}{tracked}{match.group(3)}"

    html = HREF_RE.sub(replace, html)
    pixel = urljoin(settings.public_base_url.rstrip("/") + "/", f"t/open/{delivery.tracking_token}.gif")
    unsubscribe = urljoin(settings.public_base_url.rstrip("/") + "/", f"unsubscribe/{delivery.tracking_token}")
    return (
        html
        + f'<img src="{escape(pixel)}" width="1" height="1" alt="" style="display:none">'
        + f'<p style="font-size:12px;color:#777"><a href="{escape(unsubscribe)}">Отписаться</a></p>'
    )


def send_delivery(session: Session, delivery: EmailDelivery, account: MailAccount) -> None:
    message = EmailMessage()
    message["Subject"] = delivery.subject
    message["From"] = formataddr((account.from_name or account.name, account.from_email))
    message["To"] = delivery.recipient_email
    message_id = make_msgid(domain=account.from_email.split("@")[-1])
    message["Message-ID"] = message_id
    message["X-LeadFlow-ID"] = delivery.id
    message.set_content("Это письмо содержит HTML-версию.")
    message.add_alternative(delivery.html_body, subtype="html")
    try:
        if account.smtp_security == "ssl":
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30)
        else:
            smtp = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
        with smtp:
            if account.smtp_security == "starttls":
                smtp.starttls()
            smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted))
            smtp.send_message(message)
        delivery.status = "sent"
        delivery.message_id = message_id
        delivery.sent_at = datetime.now(timezone.utc)
    except Exception as exc:
        delivery.status = "send_error"
        delivery.error = str(exc)
    session.commit()


def execute_campaign(session: Session, campaign: Campaign, settings: Settings) -> dict[str, int]:
    account = session.get(MailAccount, campaign.mailbox_id)
    template = session.get(EmailTemplate, campaign.template_id)
    if not account or not account.active or not template or not template.active:
        raise ValueError("Campaign mailbox or template is inactive")

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = session.scalar(
        select(func.count()).select_from(EmailDelivery).where(
            EmailDelivery.mailbox_id == account.id,
            EmailDelivery.sent_at >= today,
            EmailDelivery.status == "sent",
        )
    ) or 0
    available = max(0, min(campaign.daily_limit, account.daily_limit) - sent_today)
    limit = min(available, campaign.run_limit)
    if not limit:
        return {"selected": 0, "sent": 0, "errors": 0}

    query = select(Company).where(
        Company.email.is_not(None),
        Company.manually_blocked.is_(False),
        ~Company.email.in_(select(Suppression.email)),
        ~Company.id.in_(select(EmailDelivery.company_id)),
    )
    if campaign.category:
        query = query.where(Company.category == campaign.category)
    if campaign.city:
        query = query.where(Company.city == campaign.city)
    companies = list(session.scalars(query.order_by(Company.collected_at).limit(limit * 10)))

    sent = errors = 0
    for company in companies:
        if sent + errors >= limit:
            break
        recipient = normalize_email(company.email or "")
        if session.get(Suppression, recipient):
            continue
        if session.scalar(select(EmailDelivery.id).where(EmailDelivery.recipient_email == recipient)):
            continue
        subject, html = render_template(template, company)
        delivery = EmailDelivery(
            campaign_id=campaign.id,
            company_id=company.id,
            mailbox_id=account.id,
            template_id=template.id,
            recipient_email=recipient,
            subject=subject,
            html_body="",
            tracking_token=secrets.token_urlsafe(24),
        )
        session.add(delivery)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            continue
        delivery.html_body = add_tracking(session, delivery, html, settings)
        session.commit()
        send_delivery(session, delivery, account)
        sent += delivery.status == "sent"
        errors += delivery.status == "send_error"
    return {"selected": int(sent + errors), "sent": int(sent), "errors": int(errors)}


def test_smtp(account: MailAccount) -> None:
    if account.smtp_security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=20)
    else:
        smtp = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=20)
    with smtp:
        if account.smtp_security == "starttls":
            smtp.starttls()
        smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted))


def test_imap(account: MailAccount) -> None:
    with imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=20) as imap:
        imap.login(account.imap_login, decrypt_secret(account.imap_password_encrypted))
