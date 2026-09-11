from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Company, WebsiteAnalysis
from app.services.deepseek import DeepSeekClient, DeepSeekError, ai_settings
from app.services.email_discovery import BAD_DOMAINS, BAD_TLDS, EMAIL_RE
from app.services.provenance import apply_field


RELEVANT_WORDS = (
    "contact", "contacts", "about", "company", "team", "staff", "management", "leadership",
    "offices", "branches", "services", "контакт", "о-компании", "команда", "руковод",
    "филиал", "офис", "реквизит", "услуг",
)
BUSINESS_FIELDS = (
    "company_email", "company_phone", "region", "branches_count", "decision_maker_name",
    "decision_maker_position", "decision_maker_email", "decision_maker_phone", "inn",
)
PHONE_RE = re.compile(r"(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}")
INN_RE = re.compile(r"(?:ИНН\s*[:№]?\s*)(\d{10}|\d{12})", re.IGNORECASE)
BRANCH_RE = re.compile(r"\b(\d{1,4})\s+(?:филиал(?:а|ов)?|офис(?:а|ов)?|представительств(?:о|а)?)\b", re.IGNORECASE)
MANAGER_RE = re.compile(
    r"(?:генеральн(?:ый|ого) директор(?:а)?|директор|руководитель)\s*[:—-]?\s*"
    r"([А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2})"
)
DECISION_ROLE_RE = re.compile(
    r"генеральн(?:ый|ого) директор|коммерческ(?:ий|ого) директор|директор по закупкам|"
    r"руководител[ья]|управляющ(?:ий|ая|его)|владел(?:ец|ьца)|основател[ья]|учредител[ья]|закуп",
    re.IGNORECASE,
)
BOILERPLATE_RE = re.compile(r"cookie|куки|файл(?:ы|ов)? cookie|политик[аи] конфиденциальности", re.IGNORECASE)


@dataclass(slots=True)
class WebsitePage:
    url: str
    text: str
    html: str = ""
    raw_chars: int = 0


@dataclass(slots=True)
class WebsiteSnapshot:
    pages: list[WebsitePage] = field(default_factory=list)

    @property
    def allowed_urls(self) -> set[str]:
        return {page.url for page in self.pages}

    @property
    def raw_chars(self) -> int:
        return sum(page.raw_chars or len(page.html) for page in self.pages)

    @property
    def cleaned_chars(self) -> int:
        return sum(len(page.text) for page in self.pages)

    @property
    def content_hash(self) -> str:
        body = "\n".join(f"{page.url}\n{page.text}" for page in self.pages)
        return hashlib.sha256(body.encode()).hexdigest()

    def compact_blocks(self, max_chars: int) -> list[dict[str, str]]:
        if not self.pages or max_chars <= 0:
            return []
        quota = max(1, max_chars // len(self.pages))
        blocks = [{"url": page.url, "text": page.text[:quota]} for page in self.pages if page.text]
        remaining = max_chars - sum(len(block["text"]) for block in blocks)
        for page, block in zip((page for page in self.pages if page.text), blocks, strict=True):
            if remaining <= 0:
                break
            extra = page.text[len(block["text"]):len(block["text"]) + remaining]
            block["text"] += extra
            remaining -= len(extra)
        return blocks


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    value: str | int
    source_url: str
    evidence_text: str


class AIEnrichmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict[str, str | int | None]
    evidence: list[Evidence]


def _is_public_host(hostname: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False
    return True


def _clean_html(html: str) -> tuple[str, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script:not([type='application/ld+json']),style,noscript,svg,nav,footer,iframe,form"):
        tag.decompose()
    for node in list(soup.find_all(string=BOILERPLATE_RE)):
        parent = node.parent
        if parent and parent.name in {"div", "section", "aside"}:
            parent.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)), soup


def crawl_website(site_url: str, *, max_pages: int = 6) -> WebsiteSnapshot:
    start = site_url if "://" in site_url else "https://" + site_url
    parsed = urlsplit(start)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not _is_public_host(parsed.hostname):
        return WebsiteSnapshot()
    origin = parsed.hostname.casefold().removeprefix("www.")
    queue = [start]
    visited: set[str] = set()
    pages: list[WebsitePage] = []
    headers = {"User-Agent": "LeadFlow/1.0 public-company-analysis", "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=15) as client:
        while queue and len(pages) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                response = client.get(url)
                response.raise_for_status()
                final = urlsplit(str(response.url))
                if (
                    "text/html" not in response.headers.get("content-type", "")
                    or not final.hostname
                    or final.hostname.casefold().removeprefix("www.") != origin
                ):
                    continue
            except (httpx.HTTPError, ValueError):
                continue
            resolved = str(response.url).split("#", 1)[0]
            text, soup = _clean_html(response.text)
            if not text:
                continue
            pages.append(WebsitePage(resolved, text[:25_000], response.text, len(response.text)))
            ranked: list[tuple[int, str]] = []
            for link in soup.select("a[href]"):
                href = str(link.get("href") or "")
                target = urljoin(resolved, href).split("#", 1)[0]
                target_parts = urlsplit(target)
                label = f"{href} {link.get_text(' ')}".casefold()
                if (
                    target_parts.scheme not in {"http", "https"}
                    or not target_parts.hostname
                    or target_parts.hostname.casefold().removeprefix("www.") != origin
                    or target in visited or target in queue
                ):
                    continue
                score = sum(word in label for word in RELEVANT_WORDS)
                if score:
                    ranked.append((-score, target))
            queue.extend(target for _, target in sorted(set(ranked)))
    return WebsiteSnapshot(pages)


def _jsonld_nodes(document: object) -> list[dict]:
    pending = document if isinstance(document, list) else [document]
    result: list[dict] = []
    while pending:
        node = pending.pop(0)
        if not isinstance(node, dict):
            continue
        result.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            pending.extend(graph)
    return result


def _deterministic(snapshot: WebsiteSnapshot) -> dict[str, tuple[object, str, float, str]]:
    found: dict[str, tuple[object, str, float, str]] = {}
    emails: list[tuple[float, str, str]] = []
    for page in snapshot.pages:
        soup = BeautifulSoup(page.html, "html.parser")
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                document = json.loads(script.string or script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue
            for node in _jsonld_nodes(document):
                node_type = node.get("@type") or ""
                types = node_type if isinstance(node_type, list) else [node_type]
                if not any(kind in {"Organization", "LocalBusiness", "Corporation"} for kind in types):
                    continue
                structured: dict[str, object | None] = {
                    "company_email": node.get("email"), "company_phone": node.get("telephone"),
                    "inn": node.get("taxID") or node.get("vatID"),
                }
                if isinstance(node.get("address"), dict):
                    structured["region"] = node["address"].get("addressRegion")
                branches = node.get("department") or node.get("subOrganization")
                if isinstance(branches, list) and len(branches) > 1:
                    structured["branches_count"] = len(branches)
                for name, value in structured.items():
                    if value not in (None, "") and name not in found:
                        found[name] = (value, page.url, 0.99, "website_structured_data")
        mailtos = {str(link.get("href"))[7:].split("?", 1)[0].strip().casefold() for link in soup.select('a[href^="mailto:"]')}
        for candidate in mailtos | {item.casefold() for item in EMAIL_RE.findall(page.text)}:
            domain = candidate.rsplit("@", 1)[-1]
            if "@" in candidate and domain not in BAD_DOMAINS and domain.rsplit(".", 1)[-1] not in BAD_TLDS:
                emails.append((1.0 if candidate in mailtos else 0.85, candidate, page.url))
        for name, regex, converter, confidence in (
            ("company_phone", PHONE_RE, lambda m: m.group(0), 0.85),
            ("inn", INN_RE, lambda m: m.group(1), 0.99),
            ("branches_count", BRANCH_RE, lambda m: int(m.group(1)), 0.9),
            ("decision_maker_name", MANAGER_RE, lambda m: m.group(1), 0.9),
        ):
            if name not in found and (match := regex.search(page.text)):
                found[name] = (converter(match), page.url, confidence, "website_regex")
    if emails and "company_email" not in found:
        confidence, email, url = max(emails)
        found["company_email"] = (email, url, confidence, "website_regex")
    return found


def _facts(snapshot: WebsiteSnapshot, limit: int = 5) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    seen: set[str] = set()
    for page in snapshot.pages:
        for sentence in re.split(r"(?<=[.!?])\s+", page.text):
            sentence = sentence.strip()
            if 45 <= len(sentence) <= 240 and sentence.casefold() not in seen:
                seen.add(sentence.casefold())
                facts.append({"text": sentence, "source_url": page.url})
                if len(facts) == limit:
                    return facts
    return facts


def _ai_schema(missing_fields: list[str]) -> dict:
    value_properties = {
        name: ({"type": ["integer", "null"], "minimum": 1} if name == "branches_count" else {"type": ["string", "null"]})
        for name in missing_fields
    }
    return {
        "type": "object", "additionalProperties": False, "required": ["fields", "evidence"],
        "properties": {
            "fields": {"type": "object", "additionalProperties": False, "required": missing_fields, "properties": value_properties},
            "evidence": {
                "type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["field", "value", "source_url", "evidence_text"], "properties": {
                    "field": {"type": "string", "enum": missing_fields}, "value": {"type": ["string", "integer"]},
                    "source_url": {"type": "string", "maxLength": 500},
                    "evidence_text": {"type": "string", "maxLength": 220},
                }},
            },
        },
    }


AI_INSTRUCTIONS = """Извлеки только запрошенные пустые поля из предоставленных фрагментов публичного сайта.
Не используй внешние знания и не делай предположений. Не создавай email по имени, не угадывай телефон, ИНН,
должность, сайт или число филиалов. Если точного подтверждения нет, верни null. Для каждого непустого
значения добавь короткую дословную цитату до 220 символов и URL из контекста. ЛПР указывай только для явно
названного владельца, основателя, управляющего, генерального/коммерческого директора или руководителя закупок.
Шеф-повар, автор меню и другие сотрудники без управленческой роли не являются ЛПР. Ответ — только JSON по схеме."""
ENRICHMENT_PROMPT_VERSION = "website-enrichment-v3"


def _validated_ai_values(result: AIEnrichmentResult, snapshot: WebsiteSnapshot, missing: list[str]) -> dict[str, tuple[object, str, str]]:
    if set(result.fields) != set(missing):
        raise ValueError("ИИ вернул незапрошенные поля")
    evidence = {(item.field, str(item.value)): item for item in result.evidence}
    pages = {page.url: page.text for page in snapshot.pages}
    accepted: dict[str, tuple[object, str, str]] = {}
    for name, value in result.fields.items():
        if value in (None, ""):
            continue
        item = evidence.get((name, str(value)))
        if not item or item.source_url not in pages or item.evidence_text not in pages[item.source_url]:
            continue
        if name in {"company_email", "decision_maker_email"} and str(value).casefold() not in item.evidence_text.casefold():
            continue
        if name == "branches_count" and not any(int(match.group(1)) == int(value) for match in BRANCH_RE.finditer(item.evidence_text)):
            continue
        if name == "inn" and not re.fullmatch(r"\d{10}|\d{12}", str(value)):
            continue
        accepted[name] = (value, item.source_url, item.evidence_text)
    decision_context = " ".join(
        str(accepted[name][0]) + " " + accepted[name][2]
        for name in ("decision_maker_name", "decision_maker_position") if name in accepted
    )
    if decision_context and not DECISION_ROLE_RE.search(decision_context):
        accepted.pop("decision_maker_name", None)
        accepted.pop("decision_maker_position", None)
    return accepted


class WebsiteAnalysisService:
    def __init__(self, session: Session, settings: Settings | None = None, *, deepseek: DeepSeekClient | None = None):
        self.session = session
        self.settings = settings or Settings()
        self.deepseek = deepseek or DeepSeekClient(session, self.settings)

    def enrich(self, company: Company, *, use_ai: bool = True, refresh: bool = False, ai_config: object | None = None) -> dict:
        del ai_config  # legacy argument: credentials are intentionally accepted only from environment.
        if not company.website:
            return {"pages": 0, "deterministic": 0, "ai": 0, "ai_called": False, "missing_fields": []}
        previous = self.session.scalar(select(WebsiteAnalysis).where(WebsiteAnalysis.company_id == company.id))
        reused_website_cache = previous is not None and not refresh and previous.website == company.website
        snapshot = crawl_website(company.website) if not reused_website_cache else WebsiteSnapshot([
            WebsitePage(block["url"], block["text"], "", 0) for block in previous.page_blocks
        ])
        if not snapshot.pages:
            return {"pages": 0, "deterministic": 0, "ai": 0, "ai_called": False, "missing_fields": [], "error": "Сайт недоступен."}
        deterministic = 0
        deterministic_values: dict[str, object] = dict(previous.deterministic_fields or {}) if reused_website_cache else {}
        extracted = {} if reused_website_cache else _deterministic(snapshot)
        for name, (value, url, confidence, method) in extracted.items():
            deterministic_values[name] = value
            if getattr(company, name, None) in (None, ""):
                deterministic += int(apply_field(self.session, company, name, value, discovery_method=method, source_url=url, confidence=confidence))
        blocks = snapshot.compact_blocks(self.settings.deepseek_max_input_chars)
        content_hash = previous.content_hash if reused_website_cache else snapshot.content_hash
        if previous is None:
            previous = WebsiteAnalysis(company_id=company.id, website=company.website, content_hash=content_hash)
            self.session.add(previous)
        changed = previous.content_hash != content_hash
        previous.website = company.website
        previous.content_hash = content_hash
        if not reused_website_cache:
            previous.raw_chars = snapshot.raw_chars
            previous.cleaned_chars = snapshot.cleaned_chars
        previous.relevant_chars = sum(len(block["text"]) for block in blocks)
        previous.page_blocks = blocks
        previous.facts = _facts(snapshot)
        previous.deterministic_fields = deterministic_values
        if not reused_website_cache:
            previous.analyzed_at = datetime.now(timezone.utc)
        if changed:
            previous.ai_enrichment_result = {}
        self.session.flush()

        missing = [name for name in BUSINESS_FIELDS if getattr(company, name, None) in (None, "")]
        result = {"pages": len(snapshot.pages), "deterministic": deterministic, "ai": 0, "ai_called": False,
                  "missing_fields": missing, "content_hash": content_hash, "raw_chars": snapshot.raw_chars,
                  "cleaned_chars": previous.cleaned_chars, "relevant_chars": previous.relevant_chars,
                  "website_cache_hit": reused_website_cache}
        settings_row = ai_settings(self.session)
        stored_ai = previous.ai_enrichment_result or {}
        attempted = set(stored_ai.get("attempted_fields") or []) if stored_ai.get("prompt_version") == ENRICHMENT_PROMPT_VERSION else set()
        requested = [name for name in missing if name not in attempted]
        if not use_ai or not settings_row.enrichment_enabled or not requested:
            self.session.commit()
            result["skipped"] = "cache" if missing and not requested else "disabled_or_complete"
            return result
        known_fields = {name: getattr(company, name) for name in BUSINESS_FIELDS if getattr(company, name, None) not in (None, "")}
        prompt = json.dumps({"company_name": company.company_name, "city": company.city, "website": company.website,
                             "known_fields": known_fields, "missing_fields": requested, "page_blocks": blocks},
                            ensure_ascii=False, separators=(",", ":"))
        result["ai_called"] = True
        try:
            generated = self.deepseek.generate(
                company_id=company.id, operation="website_enrichment", content_hash=content_hash,
                missing_fields=requested, prompt_version=ENRICHMENT_PROMPT_VERSION, instructions=AI_INSTRUCTIONS,
                input_text=prompt, response_model=AIEnrichmentResult, schema=_ai_schema(requested), max_output_tokens=320,
            )
            accepted = _validated_ai_values(generated.data, snapshot, requested)
            ai_count = 0
            for name, (value, url, evidence_text) in accepted.items():
                if getattr(company, name, None) in (None, ""):
                    ai_count += int(apply_field(self.session, company, name, value, discovery_method="ai_website_analysis",
                                                source_url=url, confidence=0.8))
            previous.ai_enrichment_result = {"prompt_version": ENRICHMENT_PROMPT_VERSION,
                                              "attempted_fields": sorted(attempted | set(requested)),
                                              "fields": {name: value[0] for name, value in accepted.items()},
                                              "evidence": [item.model_dump() for item in generated.data.evidence]}
            result.update({"ai": ai_count, "ai_called": not generated.cache_hit, "ai_cache_hit": generated.cache_hit,
                           "fields_found": sorted(accepted), "input_tokens": generated.usage.input_tokens,
                           "output_tokens": generated.usage.output_tokens, "request_key": generated.request_key})
        except (DeepSeekError, ValueError) as exc:
            result["ai_error"] = str(exc)
        self.session.commit()
        return result


class CompanyEnrichmentService(WebsiteAnalysisService):
    """Backwards-compatible name for the shared website analysis service."""
