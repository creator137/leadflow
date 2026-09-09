from __future__ import annotations

from typing import Protocol

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIConfig, Company, EmailDelivery, EmailTemplate, MailAccount
from app.services.mailing import build_delivery
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


class PersonalizationProvider(Protocol):
    def generate(self, company: Company) -> str: ...


class ConfiguredAIProvider:
    def __init__(self, config: AIConfig): self.config = config

    def generate(self, company: Company) -> str:
        config = self.config
        prompt = config.prompt_template.format(
            company_name=company.company_name, city=company.city or "", category=company.category or "",
            website=company.website or "", website_content=_website_context(company.website),
        )
        response = httpx.post(config.api_base.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {decrypt_secret(config.api_key_encrypted)}"}, json={"model": config.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.4}, timeout=60)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()


def generate_text(company: Company, config: AIConfig) -> str:
    return ConfiguredAIProvider(config).generate(company)


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
    personalized_text = generate_text(company, ai_config)
    return build_delivery(session, company, account, template, settings, send_mode="personalized", extra={"personalized_text": personalized_text})
