from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Company, CompanyDirection, Direction, DirectionProposalTemplate, EmailDelivery, EmailTemplate,
    GoogleSheetsConfig, MailAccount, SenderSettings, SheetPersonalizationDraft, WebsiteAnalysis,
)
from app.services.company_enrichment import WebsiteAnalysisService
from app.services.deepseek import DeepSeekClient, DeepSeekError, ai_settings
from app.services.mailing import build_delivery
from app.services.attachments import active_attachments


DEFAULT_SIGNATURE = """С уважением,
Агеева Юлия
специалист по развитию
+7 916 208-66-28
zakaz-1.1@bogp.ru
www.bogorodsk-pryanik.ru"""

AI_INSTRUCTION = (
    "Найди только подтверждённые факты сайта, которые помогают аккуратно связать предложение фабрики "
    "«Богородский пряник» с деятельностью компании. Напиши не более двух коротких естественных абзацев. "
    "Не выдумывай события, клиентов, масштабы, филиалы, достижения, должности или планы."
)


@dataclass(frozen=True)
class TemplateCopy:
    subject: str
    main_body: str
    extra_block: str = ""
    cta: str = "Если формат вам подходит, могу отправить примеры работ и предложить несколько вариантов под ваши задачи."


DEFAULTS: dict[str, TemplateCopy] = {
    "рестораны": TemplateCopy(
        "Брендированные пряники для гостей и мероприятий",
        "Для ресторанов мы делаем брендированные пряники и подарочные наборы с логотипом или символикой заведения. Их можно использовать как комплимент гостям, дополнение к банкетам и мероприятиям, сезонные подарки или небольшие памятные наборы.\n\nМожем разработать индивидуальную форму, рисунок или надпись под стиль вашего заведения. Работаем как с небольшими, так и с крупными партиями и доставляем заказы по России.",
    ),
    "кафе": TemplateCopy(
        "Брендированные пряники для гостей вашего кафе",
        "Для кафе мы создаём пряники с логотипом и подарочные наборы, которые можно предложить как комплимент к заказу, добавить в упаковку навынос или подарить постоянным гостям.\n\nРазрабатываем сезонные коллекции, индивидуальные рисунки и надписи. Выпускаем небольшие и крупные партии и доставляем по России.",
    ),
    "отели": TemplateCopy(
        "Идея для welcome-подарков и мероприятий",
        "Для отелей мы делаем брендированные пряники и подарочные наборы: welcome-подарки, комплименты в номер, сувениры гостям и дополнения для конференций, свадеб и корпоративных групп.\n\nМожем разработать форму, рисунок и упаковку в стиле отеля. Работаем с разными объёмами и доставляем заказы по России.",
    ),
    "event-агентства": TemplateCopy(
        "Брендированные подарки для ваших мероприятий",
        "Для событий мы создаём пряники и подарочные наборы с дизайном под концепцию мероприятия. Это могут быть подарки гостям, welcome-наборы, комплименты спикерам и партнёрам.\n\nРазрабатываем индивидуальную форму, рисунок, надпись и упаковку. Берём небольшие и крупные партии, доставляем по России.",
    ),
    "кейтеринг": TemplateCopy(
        "Комплименты и подарки для мероприятий",
        "Для кейтеринговых проектов мы делаем брендированные пряники, комплименты гостям и подарочные коробки под конкретное мероприятие или компанию-заказчика.\n\nПодготовим индивидуальный дизайн и нужный объём — от небольшой партии до сопровождения крупного корпоративного события. Доставляем по России.",
    ),
    "туроператоры": TemplateCopy(
        "Сувенирные наборы для гостей и партнёров",
        "Для туристических компаний мы создаём сувенирные пряники и welcome-наборы с символикой региона, маршрута или бренда. Их можно дарить группам, включать в поездку или использовать как подарок партнёрам.\n\nРазрабатываем индивидуальные формы, рисунки и надписи, выпускаем партии разного объёма и доставляем по России.",
    ),
    "школы": TemplateCopy(
        "Памятные пряники для школьных праздников",
        "Для школ мы делаем памятные пряники и подарочные наборы к выпускным, праздникам и важным школьным событиям — для учеников, учителей и родителей.\n\nМожно использовать символику школы, имена, индивидуальные надписи и рисунки. Работаем с небольшими и крупными партиями, доставляем по России.",
    ),
}
EXPECTED_DIRECTIONS = ("Рестораны", "Кафе", "Отели", "Event-агентства", "Кейтеринг", "Туроператоры", "Школы")


def default_copy(direction_name: str) -> TemplateCopy:
    key = direction_name.strip().casefold().replace("ё", "е")
    for name, value in DEFAULTS.items():
        if name.replace("ё", "е") in key or key in name.replace("ё", "е"):
            return value
    return TemplateCopy(
        "Брендированные пряники и подарочные наборы",
        f"Для организаций направления «{direction_name}» мы создаём пряники и подарочные наборы с логотипом, индивидуальной формой, рисунком или надписью. Их можно использовать для гостей, мероприятий, корпоративных подарков и памятных наборов.\n\nРаботаем с небольшими и крупными партиями и доставляем заказы по России.",
    )


def ensure_proposal_template(session: Session, direction: Direction) -> DirectionProposalTemplate:
    row = session.scalar(select(DirectionProposalTemplate).where(DirectionProposalTemplate.direction_id == direction.id))
    if row:
        return row
    copy = default_copy(direction.name)
    row = DirectionProposalTemplate(
        direction_id=direction.id, subject=copy.subject, greeting="Здравствуйте!", main_body=copy.main_body,
        extra_block=copy.extra_block, cta=copy.cta, signature="",
        ai_instruction=AI_INSTRUCTION, ai_personalization_enabled=True,
    )
    session.add(row)
    session.flush()
    return row


def get_sender_settings(session: Session) -> SenderSettings:
    row = session.get(SenderSettings, "default")
    if row:
        return row
    row = SenderSettings(
        id="default", display_name="Агеева Юлия", position="специалист по развитию",
        company_name="Богородский пряник", phone="+7 916 208-66-28",
        email="zakaz-1.1@bogp.ru", website="https://www.bogorodsk-pryanik.ru",
        product_description=("Брендированные пряники и подарочные наборы с индивидуальной формой, "
                             "рисунком или надписью. Доставка по России."),
        logo_path="logo.jpg", signature_text=DEFAULT_SIGNATURE,
    )
    session.add(row)
    session.flush()
    return row


def default_automatic_text(direction_name: str) -> tuple[str, str]:
    copy = default_copy(direction_name)
    text = (
        "Здравствуйте!\n\n" + copy.main_body +
        "\n\n" + copy.cta +
        "\n\nС уважением,\n{{sender_name}}\n{{sender_position}}\n{{sender_phone}}\n{{sender_email}}\n{{sender_site}}"
    )
    return copy.subject, text


def ensure_direction_email_template(session: Session, direction: Direction) -> EmailTemplate:
    row = session.get(EmailTemplate, direction.automatic_template_id) if direction.automatic_template_id else None
    if not row:
        row = session.scalar(select(EmailTemplate).where(
            EmailTemplate.direction_id == direction.id,
            EmailTemplate.active.is_(True),
        ).order_by(EmailTemplate.created_at).limit(1))
    if not row:
        subject, text = default_automatic_text(direction.name)
        escaped = html.escape(text).replace("\n", "<br>")
        # Restore Jinja variables after escaping; values remain autoescaped at render time.
        escaped = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"{{ \1 }}", escaped)
        row = EmailTemplate(
            name=f"Автоматическое письмо — {direction.name}", direction_id=direction.id,
            subject_template=subject, text_template=text,
            html_template=f'<div style="font-family:Arial,sans-serif;font-size:16px;line-height:1.55">{escaped}</div>',
            active=True,
        )
        session.add(row)
        session.flush()
    direction.automatic_template_id = row.id
    return row


def ensure_all_proposal_templates(session: Session) -> None:
    changed = False
    existing_names = {name.casefold() for name in session.scalars(select(Direction.name).where(Direction.archived_at.is_(None)))}
    existing_slugs = set(session.scalars(select(Direction.slug)))
    for name in EXPECTED_DIRECTIONS:
        if name.casefold() in existing_names:
            continue
        base_slug = re.sub(r"[^a-zа-яё0-9]+", "-", name.casefold(), flags=re.I).strip("-")
        slug, suffix = base_slug, 2
        while slug in existing_slugs:
            slug, suffix = f"{base_slug}-{suffix}", suffix + 1
        session.add(Direction(name=name, slug=slug, sheet_tab=name, active=False, limit_new=50))
        existing_names.add(name.casefold()); existing_slugs.add(slug); changed = True
    if changed:
        session.flush()
    for direction in session.scalars(select(Direction).where(Direction.archived_at.is_(None))):
        if not session.scalar(select(DirectionProposalTemplate.id).where(DirectionProposalTemplate.direction_id == direction.id)):
            ensure_proposal_template(session, direction)
            changed = True
        if not direction.automatic_template_id:
            ensure_direction_email_template(session, direction)
            changed = True
    if not session.get(SenderSettings, "default"):
        get_sender_settings(session)
        changed = True
    if changed:
        session.commit()


class ProposalFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: str = Field(min_length=1, max_length=400)
    evidence: str = Field(min_length=1, max_length=500)
    source_url: str = Field(pattern=r"^https?://", max_length=2000)

    @field_validator("fact", "evidence")
    @classmethod
    def no_html(cls, value: str) -> str:
        if re.search(r"<\s*/?\s*[a-z][^>]*>", value, re.I):
            raise ValueError("HTML is not allowed")
        return value.strip()


class ProposalAIResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_summary: str | None = Field(default=None, max_length=500)
    relevant_facts: list[ProposalFact] = Field(default_factory=list, max_length=2)
    personalized_intro: str | None = Field(default=None, max_length=400)
    relevance_paragraph: str | None = Field(default=None, max_length=700)
    suggested_use_cases: list[str] = Field(default_factory=list, max_length=4)
    personalized_cta_hint: str | None = Field(default=None, max_length=300)
    confidence: float = Field(ge=0, le=1)

    @field_validator("company_summary", "personalized_intro", "relevance_paragraph", "personalized_cta_hint")
    @classmethod
    def no_html(cls, value: str | None) -> str | None:
        if value and re.search(r"<\s*/?\s*[a-z][^>]*>", value, re.I):
            raise ValueError("HTML is not allowed")
        return value.strip() if value else None

    @field_validator("suggested_use_cases")
    @classmethod
    def no_html_list(cls, values: list[str]) -> list[str]:
        if any(re.search(r"<\s*/?\s*[a-z][^>]*>", value, re.I) for value in values):
            raise ValueError("HTML is not allowed")
        return [value.strip() for value in values if value.strip()]


PROPOSAL_AI_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["company_summary", "relevant_facts", "personalized_intro", "relevance_paragraph", "suggested_use_cases", "personalized_cta_hint", "confidence"],
    "properties": {
        "company_summary": {"type": ["string", "null"], "maxLength": 500},
        "relevant_facts": {"type": "array", "maxItems": 2, "items": {"type": "object", "additionalProperties": False,
            "required": ["fact", "evidence", "source_url"], "properties": {
                "fact": {"type": "string", "maxLength": 400}, "evidence": {"type": "string", "maxLength": 500},
                "source_url": {"type": "string", "maxLength": 2000}}}},
        "personalized_intro": {"type": ["string", "null"], "maxLength": 400},
        "relevance_paragraph": {"type": ["string", "null"], "maxLength": 700},
        "suggested_use_cases": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 250}},
        "personalized_cta_hint": {"type": ["string", "null"], "maxLength": 300},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

PROPOSAL_AI_PROMPT = """Верни только компактный JSON по схеме и пиши полностью на русском языке. Используй
исключительно переданные фрагменты сайта. Выбери не более двух фактов, которые прямо помогают предложить
подарки гостям, комплименты, welcome-наборы, брендирование или подарки для подтверждённых мероприятий.
Не используй адрес, телефон, часы работы и общие описания как повод для персонализации. Каждый выбранный факт
обязан содержать точную подтверждающую цитату и URL из контекста. personalized_intro начни естественно:
«Увидели, что…» или «На вашем сайте указано, что…». relevance_paragraph — короткая уместная связь факта с
предложением фабрики, без новых услуг и догадок. Всего один-два коротких абзаца. Если коммерчески полезного
факта нет, верни пустые списки и null для персональных блоков. Не пиши HTML. Не меняй цены, условия, контакты,
преимущества, подпись или основной призыв к действию."""
PROPOSAL_PROMPT_VERSION = "proposal-personalization-v2"


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _validated_evidence(result: ProposalAIResult, analysis: WebsiteAnalysis | None) -> list[dict[str, str]]:
    blocks = list(analysis.page_blocks or []) if analysis else []
    allowed: dict[str, str] = {}
    for block in blocks:
        url, text = str(block.get("url") or block.get("source_url") or ""), str(block.get("text") or "")
        if url and text:
            allowed[url.rstrip("/")] = _norm(text)
    accepted: list[dict[str, str]] = []
    for item in result.relevant_facts:
        source = item.source_url.rstrip("/")
        evidence = _norm(item.evidence)
        source_text = allowed.get(source, "")
        if source_text and evidence and evidence in source_text:
            accepted.append(item.model_dump())
    return accepted[:2]


def _personalization_text(result: ProposalAIResult, evidence: list[dict[str, str]]) -> str:
    if not evidence:
        return ""
    return "\n\n".join(value for value in (result.personalized_intro, result.relevance_paragraph) if value).strip()


def direction_for_company(session: Session, company_id: str) -> Direction | None:
    return session.scalar(
        select(Direction).join(CompanyDirection, CompanyDirection.direction_id == Direction.id)
        .where(CompanyDirection.company_id == company_id, Direction.archived_at.is_(None)).limit(1)
    )


def delivery_template_for_direction(session: Session, direction: Direction) -> EmailTemplate:
    return ensure_direction_email_template(session, direction)


def _replace_business_fields(value: str, company: Company) -> str:
    replacements = {
        "company_name": company.company_name, "city": company.city or "", "decision_maker_name": company.decision_maker_name or "",
    }
    for key, replacement in replacements.items():
        value = value.replace("{{ " + key + " }}", replacement).replace("{{" + key + "}}", replacement).replace("{" + key + "}", replacement)
    return value


def _html_paragraphs(value: str, company: Company) -> str:
    value = _replace_business_fields(value or "", company).strip()
    return "".join(
        f'<p style="margin:0 0 16px;line-height:1.55;color:#3f332b;">{html.escape(part).replace(chr(10), "<br>")}</p>'
        for part in re.split(r"\n\s*\n", value) if part.strip()
    )


def render_proposal(draft: SheetPersonalizationDraft, company: Company, settings: Settings) -> dict[str, str]:
    if draft.status == "sent" and draft.sent_html_snapshot:
        return {
            "subject": draft.subject or "", "html_body": draft.sent_html_snapshot,
            "text_body": draft.sent_text_snapshot or draft.text_body or "",
        }
    # Drafts prepared before direction proposal templates were introduced only
    # contain the rendered snapshot. Keep those previews readable until the
    # user explicitly refreshes the personalization into the editable format.
    structured_blocks = (
        draft.greeting, draft.main_body, draft.ai_personalization,
        draft.extra_block, draft.cta, draft.signature,
    )
    if not any((block or "").strip() for block in structured_blocks) and draft.html_snapshot:
        return {
            "subject": draft.subject or "",
            "html_body": draft.html_snapshot,
            "text_body": draft.text_body or "",
        }
    subject = _replace_business_fields(draft.subject or "", company).strip()
    blocks = [draft.greeting, draft.main_body, draft.ai_personalization, draft.extra_block, draft.cta]
    content = "".join(_html_paragraphs(block or "", company) for block in blocks if (block or "").strip())
    signature_html = _html_paragraphs(draft.signature or "", company)
    logo_url = f"{settings.public_base_url.rstrip('/')}/email-assets/bogorodsky-pryanik-logo.jpg"
    html_body = f'''<!doctype html><html><body style="margin:0;padding:0;background:#f7f3ef;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7f3ef;"><tr><td align="center" style="padding:20px 10px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border:1px solid #eadfd4;border-radius:12px;">
<tr><td style="padding:24px 32px 18px;border-bottom:3px solid #b06d3f;"><img src="{html.escape(logo_url)}" width="138" alt="Богородский пряник" style="display:block;width:138px;max-width:45%;height:auto;border:0;"></td></tr>
<tr><td style="padding:28px 32px 12px;font-family:Arial,sans-serif;font-size:16px;color:#3f332b;">{content}</td></tr>
<tr><td style="padding:8px 32px 28px;font-family:Arial,sans-serif;font-size:15px;color:#5b493d;border-top:1px solid #eee4db;">{signature_html}</td></tr>
</table></td></tr></table></body></html>'''
    text_blocks = [_replace_business_fields(block or "", company).strip() for block in blocks + [draft.signature] if (block or "").strip()]
    return {"subject": subject, "html_body": html_body, "text_body": "\n\n".join(text_blocks)}


def prepare_proposal_draft(
    session: Session, company: Company, settings: Settings, *, mailbox: MailAccount | None = None,
    config: GoogleSheetsConfig | None = None, command_key: str | None = None, regenerate: bool = False,
) -> SheetPersonalizationDraft:
    direction = direction_for_company(session, company.id)
    if not direction:
        raise ValueError("Направление компании не найдено.")
    business_template = ensure_proposal_template(session, direction)
    sender = get_sender_settings(session)
    legacy_template = delivery_template_for_direction(session, direction)
    command_key = command_key or hashlib.sha256(f"proposal-draft-v1:{company.id}:{business_template.version}".encode()).hexdigest()
    draft = session.scalar(select(SheetPersonalizationDraft).where(SheetPersonalizationDraft.command_key == command_key))
    if draft and draft.status in {"ready", "sent"} and not regenerate:
        return draft
    created = draft is None
    if not draft:
        draft = SheetPersonalizationDraft(
            company_id=company.id, direction_id=direction.id, config_id=config.id if config else None,
            template_id=legacy_template.id, proposal_template_id=business_template.id,
            template_version=business_template.version, mailbox_id=mailbox.id if mailbox else None,
            command_key=command_key, status="preparing",
            recipient_email=(company.decision_maker_email or company.company_email or company.email),
            attachments_snapshot=active_attachments(session, direction.id),
        )
        session.add(draft)
        session.flush()
    analysis = session.scalar(select(WebsiteAnalysis).where(WebsiteAnalysis.company_id == company.id))
    if company.website and analysis is None:
        WebsiteAnalysisService(session, settings).enrich(company, use_ai=False)
        analysis = session.scalar(select(WebsiteAnalysis).where(WebsiteAnalysis.company_id == company.id))
    ai_result = ProposalAIResult(confidence=0)
    evidence: list[dict[str, str]] = []
    request_key: str | None = None
    enabled = business_template.ai_personalization_enabled and ai_settings(session).personalization_enabled and bool(company.website)
    if enabled:
        context_blocks = list(analysis.page_blocks or [])[:3] if analysis else []
        compact_blocks = [{"url": x.get("url") or x.get("source_url"), "text": str(x.get("text") or "")[:1000]} for x in context_blocks]
        content_hash = analysis.content_hash if analysis else hashlib.sha256((company.website or company.id).encode()).hexdigest()
        prompt = json.dumps({
            "company": {"name": company.company_name, "city": company.city, "website": company.website},
            "direction": direction.name, "direction_instruction": business_template.ai_instruction,
            "public_page_blocks": compact_blocks,
        }, ensure_ascii=False, separators=(",", ":"))
        generated = DeepSeekClient(session, settings).generate(
            company_id=company.id, operation="email_personalization", content_hash=content_hash,
            missing_fields=[business_template.id], prompt_version=PROPOSAL_PROMPT_VERSION,
            instructions=PROPOSAL_AI_PROMPT, input_text=prompt, response_model=ProposalAIResult,
            schema=PROPOSAL_AI_SCHEMA, max_output_tokens=900, use_cache=not regenerate, force=regenerate,
        )
        ai_result = generated.data
        request_key = generated.request_key
        evidence = _validated_evidence(ai_result, analysis)
    draft.proposal_template_id = draft.proposal_template_id or business_template.id
    draft.template_id = legacy_template.id
    draft.template_version = draft.template_version or business_template.version
    draft.request_key = request_key
    if not draft.recipient_email:
        draft.recipient_email = company.decision_maker_email or company.company_email or company.email
    if not draft.attachments_snapshot:
        draft.attachments_snapshot = active_attachments(session, direction.id)
    if created or not draft.subject:
        draft.subject = business_template.subject
    if created or not draft.greeting:
        draft.greeting = business_template.greeting
    if created or not draft.main_body:
        draft.main_body = business_template.main_body
    draft.ai_personalization = _personalization_text(ai_result, evidence)
    if created or draft.extra_block is None:
        draft.extra_block = business_template.extra_block
    if created or not draft.cta:
        draft.cta = business_template.cta
    if created or not draft.signature:
        draft.signature = business_template.signature.strip() or sender.signature_text
    draft.ai_evidence = evidence
    draft.ai_response_data = ai_result.model_dump()
    draft.facts = evidence
    draft.status, draft.error = "ready", None
    rendered = render_proposal(draft, company, settings)
    draft.html_body, draft.text_body, draft.html_snapshot = rendered["html_body"], rendered["text_body"], rendered["html_body"]
    session.commit()
    session.refresh(draft)
    return draft


def update_proposal_draft(session: Session, draft: SheetPersonalizationDraft, values: dict[str, Any], settings: Settings) -> SheetPersonalizationDraft:
    if draft.status == "sent":
        raise ValueError("Отправленное письмо нельзя изменить.")
    editable = {"subject", "greeting", "main_body", "ai_personalization", "extra_block", "cta", "signature", "mailbox_id", "recipient_email"}
    for key, value in values.items():
        if key in editable:
            setattr(draft, key, value)
    company = session.get(Company, draft.company_id)
    if not company:
        raise ValueError("Компания не найдена.")
    rendered = render_proposal(draft, company, settings)
    draft.html_body, draft.text_body, draft.html_snapshot = rendered["html_body"], rendered["text_body"], rendered["html_body"]
    draft.status = "ready"
    session.commit()
    session.refresh(draft)
    return draft


def send_proposal_draft(
    session: Session, draft: SheetPersonalizationDraft, settings: Settings, *, recipient_override: str | None = None,
    send_mode: str = "personalized",
) -> EmailDelivery:
    if draft.delivery_id:
        delivery = session.get(EmailDelivery, draft.delivery_id)
        if delivery:
            return delivery
    if draft.status != "ready":
        raise ValueError("Сначала подготовьте и сохраните КП.")
    company = session.get(Company, draft.company_id)
    template = session.get(EmailTemplate, draft.template_id)
    mailbox = session.get(MailAccount, draft.mailbox_id) if draft.mailbox_id else None
    if not company or not template or not mailbox or not mailbox.active:
        raise ValueError("Почтовый ящик или шаблон не настроен.")
    rendered = render_proposal(draft, company, settings)
    delivery = build_delivery(
        session, company, mailbox, template, settings, direction_id=draft.direction_id, send_mode=send_mode,
        overrides=rendered, recipient_override=recipient_override or draft.recipient_email,
        attachments_snapshot=draft.attachments_snapshot or [],
        idempotency_key=f"proposal-draft:{draft.id}:{'test:' + recipient_override if recipient_override else 'send'}",
    )
    if not recipient_override:
        draft.delivery_id = delivery.id
        draft.sent_html_snapshot = delivery.html_body
        draft.sent_text_snapshot = delivery.text_body
    session.commit()
    return delivery


def finalize_proposal_delivery(session: Session, draft: SheetPersonalizationDraft, delivery: EmailDelivery) -> None:
    if delivery.status == "sent":
        draft.status = "sent"
        draft.sent_html_snapshot = delivery.html_body
        draft.sent_text_snapshot = delivery.text_body
        session.commit()


def serialize_proposal_template(template: DirectionProposalTemplate, direction: Direction) -> dict[str, Any]:
    return {
        "id": template.id, "direction_id": direction.id, "direction_name": direction.name, "version": template.version,
        "subject": template.subject, "greeting": template.greeting, "main_body": template.main_body,
        "extra_block": template.extra_block, "cta": template.cta, "signature": template.signature,
        "ai_instruction": template.ai_instruction, "ai_personalization_enabled": template.ai_personalization_enabled,
        "active": template.active, "updated_at": template.updated_at,
    }
