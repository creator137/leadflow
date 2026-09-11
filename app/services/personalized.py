from __future__ import annotations

import hashlib
import html
import json
import re

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIRequestLog, Company, CompanyDirection, Direction, EmailDelivery, EmailTemplate, MailAccount, WebsiteAnalysis
from app.services.company_enrichment import WebsiteAnalysisService
from app.services.deepseek import DeepSeekClient, DeepSeekError, ai_settings
from app.services.mailing import build_delivery, render_template_parts


class PersonalizationFragments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(max_length=100)
    intro: str = Field(max_length=220)
    personalized_paragraph: str = Field(max_length=500)


PERSONALIZATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["subject", "intro", "personalized_paragraph"],
    "properties": {
        "subject": {"type": "string", "maxLength": 100},
        "intro": {"type": "string", "maxLength": 220},
        "personalized_paragraph": {"type": "string", "maxLength": 500},
    },
}
PERSONALIZATION_INSTRUCTIONS = """Подготовь три коротких фрагмента делового письма на русском языке.
Персонализация должна следовать только из перечисленных проверенных фактов. Не добавляй сведения, которых нет
в контексте. Если полезных фактов нет, используй нейтральное обращение без догадок. Не повторяй основное
коммерческое предложение целиком. Не используй заполнители вроде «[Ваше имя]» и не обещай неподтверждённый
результат. Вступление — не более 25 слов, персональный абзац — не более 50 слов и максимум два факта.
Ответ — только JSON по схеме."""
PERSONALIZATION_PROMPT_VERSION = "email-personalization-v2"


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _direction_name(session: Session, company_id: str) -> str | None:
    return session.scalar(
        select(Direction.name).join(CompanyDirection, CompanyDirection.direction_id == Direction.id)
        .where(CompanyDirection.company_id == company_id).limit(1)
    )


def _render(template: EmailTemplate, company: Company, account: MailAccount | None, fragments: PersonalizationFragments) -> dict[str, str]:
    extra = {
        "personalized_text": fragments.personalized_paragraph,
        "personalized_intro": fragments.intro,
        "personalized_paragraph": fragments.personalized_paragraph,
        "ai_subject": fragments.subject,
    }
    subject, body_html, body_text = render_template_parts(template, company, extra=extra, account=account)
    tokens = (template.subject_template + template.html_template + (template.text_template or ""))
    if "personalized_" not in tokens:
        prefix_html = f"<p>{html.escape(fragments.intro)}</p><p>{html.escape(fragments.personalized_paragraph)}</p>"
        body_html = prefix_html + body_html
        body_text = f"{fragments.intro}\n\n{fragments.personalized_paragraph}\n\n{body_text}"
    return {"subject": fragments.subject or subject, "html_body": body_html, "text_body": body_text}


def prepare_personalization(
    session: Session, company: Company, template: EmailTemplate, settings: Settings,
    *, account: MailAccount | None = None, regenerate: bool = False,
) -> dict[str, object]:
    configured = ai_settings(session)
    if not configured.personalization_enabled:
        raise DeepSeekError("Персональные письма с ИИ отключены.", code="disabled")
    analysis = session.scalar(select(WebsiteAnalysis).where(WebsiteAnalysis.company_id == company.id))
    if analysis is None:
        WebsiteAnalysisService(session, settings).enrich(company, use_ai=False)
        analysis = session.scalar(select(WebsiteAnalysis).where(WebsiteAnalysis.company_id == company.id))
    facts = (analysis.facts if analysis else [])[:5]
    content_hash = analysis.content_hash if analysis else hashlib.sha256((company.website or company.id).encode()).hexdigest()
    context = {
        "company_name": company.company_name, "city": company.city, "direction": _direction_name(session, company.id),
        "website": company.website, "decision_maker": {
            "name": company.decision_maker_name, "position": company.decision_maker_position,
        },
        "verified_facts": facts,
        "our_offer": _plain(template.text_template or template.html_template)[:1200],
        "tone": "доброжелательный, деловой, конкретный",
    }
    prompt = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    result = DeepSeekClient(session, settings).generate(
        company_id=company.id, operation="email_personalization", content_hash=content_hash,
        missing_fields=[template.id], prompt_version=PERSONALIZATION_PROMPT_VERSION, instructions=PERSONALIZATION_INSTRUCTIONS,
        input_text=prompt, response_model=PersonalizationFragments, schema=PERSONALIZATION_SCHEMA,
        max_output_tokens=250, use_cache=not regenerate, force=regenerate,
    )
    session.commit()
    preview = _render(template, company, account, result.data)
    return {**preview, "fragments": result.data.model_dump(), "facts": facts, "request_key": result.request_key,
            "cache_hit": result.cache_hit, "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens}


def send_personalized(
    session: Session, company: Company, account: MailAccount, template: EmailTemplate,
    request_key: str, settings: Settings, *, overrides: dict[str, str | None] | None = None,
) -> EmailDelivery:
    if not account.active or not template.active:
        raise ValueError("Почтовый ящик или шаблон отключён.")
    log = session.scalar(select(AIRequestLog).where(
        AIRequestLog.request_key == request_key, AIRequestLog.company_id == company.id,
        AIRequestLog.operation == "email_personalization", AIRequestLog.success.is_(True),
    ).order_by(AIRequestLog.created_at.desc()).limit(1))
    if not log:
        raise ValueError("Сначала подготовьте персональное письмо.")
    if template.id not in (log.missing_fields or []):
        raise ValueError("Выбран другой шаблон. Подготовьте письмо ещё раз.")
    fragments = PersonalizationFragments.model_validate(log.response_data)
    rendered = _render(template, company, account, fragments)
    rendered.update({key: value for key, value in (overrides or {}).items() if value is not None})
    return build_delivery(session, company, account, template, settings, send_mode="personalized", overrides=rendered)


# Compatibility aliases for code importing the former provider. They never accept credentials.
class ConfiguredAIProvider:
    def __init__(self, *_args, **_kwargs):
        raise DeepSeekError("Используйте подготовку персонального письма через DeepSeek.", code="unsupported")
