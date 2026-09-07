from __future__ import annotations

import secrets
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIConfig, Company, EmailDelivery, EmailTemplate, MailAccount, Suppression
from app.services.mailing import add_tracking, normalize_email, render_template, send_delivery
from app.services.secrets import decrypt_secret


def _website_context(url: str | None) -> str:
    if not url:
        return ""
    try:
        response = httpx.get(url, follow_redirects=True, timeout=15, headers={"User-Agent": "LeadFlow/0.1"})
        response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", ""):
            return ""
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return " ".join(soup.get_text(" ").split())[:12_000]
    except Exception:
        return ""


def generate_text(company: Company, config: AIConfig) -> str:
    prompt = config.prompt_template.format(
        company_name=company.company_name,
        city=company.city or "",
        category=company.category or "",
        website=company.website or "",
        website_content=_website_context(company.website),
    )
    response = httpx.post(
        config.api_base.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {decrypt_secret(config.api_key_encrypted)}"},
        json={"model": config.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def send_personalized(
    session: Session,
    company: Company,
    account: MailAccount,
    template: EmailTemplate,
    ai_config: AIConfig,
    settings: Settings,
) -> EmailDelivery:
    if not account.active or not template.active or not ai_config.active:
        raise ValueError("Mailbox, template, or AI configuration is inactive")
    if not company.email:
        raise ValueError("Company has no email")
    recipient = normalize_email(company.email)
    if session.get(Suppression, recipient) or company.manually_blocked:
        raise ValueError("Recipient is suppressed")
    if session.scalar(select(EmailDelivery.id).where(EmailDelivery.recipient_email == recipient)):
        raise ValueError("Recipient has already been contacted")
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = session.scalar(
        select(func.count()).select_from(EmailDelivery).where(
            EmailDelivery.mailbox_id == account.id,
            EmailDelivery.sent_at >= today,
            EmailDelivery.status == "sent",
        )
    ) or 0
    if sent_today >= account.daily_limit:
        raise ValueError("Mailbox daily limit has been reached")
    personalized_text = generate_text(company, ai_config)
    subject, html = render_template(template, company, {"personalized_text": personalized_text})
    delivery = EmailDelivery(
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
    except IntegrityError as exc:
        session.rollback()
        raise ValueError("Company has already been contacted") from exc
    delivery.html_body = add_tracking(session, delivery, html, settings)
    session.commit()
    send_delivery(session, delivery, account)
    return delivery
