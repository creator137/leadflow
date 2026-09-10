from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.models import AIConfig, Company
from app.services.email_discovery import BAD_DOMAINS, BAD_TLDS, EMAIL_RE
from app.services.provenance import apply_field
from app.services.secrets import decrypt_secret

RELEVANT_WORDS = (
    "contact", "contacts", "about", "company", "team", "staff", "management", "offices", "branches",
    "контакт", "о-компании", "команда", "руковод", "филиал", "офис", "реквизит",
)
PHONE_RE = re.compile(r"(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}")
INN_RE = re.compile(r"(?:ИНН\s*[:№]?\s*)(\d{10}|\d{12})", re.IGNORECASE)
BRANCH_RE = re.compile(r"\b(\d{1,4})\s+(?:филиал(?:а|ов)?|офис(?:а|ов)?)\b", re.IGNORECASE)


@dataclass(slots=True)
class WebsitePage:
    url: str
    text: str
    html: str


@dataclass(slots=True)
class WebsiteSnapshot:
    pages: list[WebsitePage] = field(default_factory=list)

    @property
    def allowed_urls(self) -> set[str]:
        return {page.url for page in self.pages}

    def prompt_content(self, max_chars: int = 50_000) -> str:
        content = "\n\n".join(f"SOURCE URL: {page.url}\n{page.text}" for page in self.pages)
        return content[:max_chars]


class EnrichmentSources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str | None = None
    branches_count: str | None = None
    decision_maker_name: str | None = None
    decision_maker_position: str | None = None
    decision_maker_email: str | None = None
    decision_maker_phone: str | None = None
    inn: str | None = None


class EnrichmentConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: float | None = Field(default=None, ge=0, le=1)
    branches_count: float | None = Field(default=None, ge=0, le=1)
    decision_maker_name: float | None = Field(default=None, ge=0, le=1)
    decision_maker_position: float | None = Field(default=None, ge=0, le=1)
    decision_maker_email: float | None = Field(default=None, ge=0, le=1)
    decision_maker_phone: float | None = Field(default=None, ge=0, le=1)
    inn: float | None = Field(default=None, ge=0, le=1)


class AIEnrichmentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str | None = None
    branches_count: int | None = Field(default=None, ge=1)
    decision_maker_name: str | None = None
    decision_maker_position: str | None = None
    decision_maker_email: str | None = None
    decision_maker_phone: str | None = None
    inn: str | None = Field(default=None, pattern=r"^(?:\d{10}|\d{12})$")
    sources: EnrichmentSources
    confidence: EnrichmentConfidence

    @model_validator(mode="after")
    def require_source_for_every_value(self):
        for field_name in (
            "region", "branches_count", "decision_maker_name", "decision_maker_position",
            "decision_maker_email", "decision_maker_phone", "inn",
        ):
            if getattr(self, field_name) is not None and not getattr(self.sources, field_name):
                raise ValueError(f"{field_name} has no source URL")
            if getattr(self, field_name) is not None and getattr(self.confidence, field_name) is None:
                raise ValueError(f"{field_name} has no confidence")
        return self


def crawl_website(site_url: str, *, max_pages: int = 8) -> WebsiteSnapshot:
    start = site_url if "://" in site_url else "https://" + site_url
    origin = urlsplit(start).netloc.casefold().removeprefix("www.")
    queue = [start]
    visited: set[str] = set()
    pages: list[WebsitePage] = []
    headers = {"User-Agent": "LeadFlow/0.2 public-company-enrichment", "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=15) as client:
        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                response = client.get(url)
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", ""):
                    continue
            except Exception:
                continue
            resolved = str(response.url).split("#", 1)[0]
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
            pages.append(WebsitePage(resolved, text[:25_000], response.text))
            links = BeautifulSoup(response.text, "html.parser").select("a[href]")
            ranked: list[tuple[int, str]] = []
            for link in links:
                href = link.get("href", "")
                target = urljoin(resolved, href).split("#", 1)[0]
                label = f"{href} {link.get_text(' ')}".casefold()
                if urlsplit(target).netloc.casefold().removeprefix("www.") != origin or target in visited or target in queue:
                    continue
                score = sum(word in label for word in RELEVANT_WORDS)
                if score:
                    ranked.append((-score, target))
            queue.extend(target for _, target in sorted(ranked))
    return WebsiteSnapshot(pages)


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
            queue = document if isinstance(document, list) else [document]
            while queue:
                node = queue.pop(0)
                if not isinstance(node, dict):
                    continue
                graph = node.get("@graph")
                if isinstance(graph, list):
                    queue.extend(graph)
                node_type = node.get("@type") or ""
                types = node_type if isinstance(node_type, list) else [node_type]
                if not any(kind in {"Organization", "LocalBusiness", "Corporation"} for kind in types):
                    continue
                structured = {
                    "company_email": node.get("email"),
                    "company_phone": node.get("telephone"),
                    "inn": node.get("taxID") or node.get("vatID"),
                }
                address = node.get("address")
                if isinstance(address, dict):
                    structured["region"] = address.get("addressRegion")
                branches = node.get("department") or node.get("subOrganization")
                if isinstance(branches, list) and len(branches) > 1:
                    structured["branches_count"] = len(branches)
                for field_name, value in structured.items():
                    if value not in (None, "") and field_name not in found:
                        found[field_name] = (value, page.url, 0.99, "website_structured_data")
        mailtos = {link.get("href", "")[7:].split("?", 1)[0].strip().casefold() for link in soup.select('a[href^="mailto:"]')}
        for candidate in mailtos | {item.casefold() for item in EMAIL_RE.findall(page.text)}:
            domain = candidate.rsplit("@", 1)[-1]
            if "@" in candidate and domain not in BAD_DOMAINS and domain.rsplit(".", 1)[-1] not in BAD_TLDS:
                confidence = 1.0 if candidate in mailtos else 0.85
                emails.append((confidence, candidate, page.url))
        if "company_phone" not in found and (match := PHONE_RE.search(page.text)):
            found["company_phone"] = (match.group(0), page.url, 0.85, "website_regex")
        if "inn" not in found and (match := INN_RE.search(page.text)):
            found["inn"] = (match.group(1), page.url, 0.99, "website_regex")
        if "branches_count" not in found and (match := BRANCH_RE.search(page.text)):
            found["branches_count"] = (int(match.group(1)), page.url, 0.9, "website_regex")
    if emails and "company_email" not in found:
        confidence, email, url = max(emails)
        found["company_email"] = (email, url, confidence, "website_regex")
    return found


SYSTEM_PROMPT = """Ты извлекаешь факты о компании из предоставленного содержимого публичного сайта.
Используй исключительно информацию, присутствующую в предоставленном содержимом сайта.
Не используй догадки и общие знания о компании. Если значение отсутствует или нельзя уверенно установить — верни null.
Не генерируй email по шаблону firstname.lastname@domain. Не угадывай телефон или имя ЛПР.
Не делай вывод о количестве филиалов без явного подтверждения. Для каждого непустого значения укажи точный SOURCE URL.
Верни только JSON, соответствующий заданной schema."""


def analyze_with_ai(snapshot: WebsiteSnapshot, config: AIConfig) -> AIEnrichmentResult:
    schema = AIEnrichmentResult.model_json_schema()
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": snapshot.prompt_content()},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "company_enrichment", "strict": True, "schema": schema}},
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {decrypt_secret(config.api_key_encrypted)}"}
    response = httpx.post(f"{config.api_base.rstrip('/')}/chat/completions", json=payload, headers=headers, timeout=90)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    result = AIEnrichmentResult.model_validate_json(content)
    for field_name in result.sources.model_fields:
        source_url = getattr(result.sources, field_name)
        if source_url and source_url not in snapshot.allowed_urls:
            raise ValueError(f"AI returned an unknown source URL for {field_name}")
    return result


class CompanyEnrichmentService:
    def __init__(self, session: Session):
        self.session = session

    def enrich(self, company: Company, *, ai_config: AIConfig | None = None) -> dict[str, int]:
        if not company.website:
            return {"pages": 0, "deterministic": 0, "ai": 0}
        snapshot = crawl_website(company.website)
        deterministic = 0
        for field_name, (value, source_url, confidence, method) in _deterministic(snapshot).items():
            deterministic += int(apply_field(
                self.session, company, field_name, value, discovery_method=method,
                source_url=source_url, confidence=confidence,
            ))
        ai_count = 0
        if ai_config and snapshot.pages:
            result = analyze_with_ai(snapshot, ai_config)
            for field_name in (
                "region", "decision_maker_name", "decision_maker_position",
                "decision_maker_email", "decision_maker_phone", "inn",
            ):
                value = getattr(result, field_name)
                if value is not None:
                    ai_count += int(apply_field(
                        self.session, company, field_name, value,
                        discovery_method="ai_website_analysis",
                        source_url=getattr(result.sources, field_name),
                        confidence=getattr(result.confidence, field_name),
                    ))
        self.session.commit()
        return {"pages": len(snapshot.pages), "deterministic": deterministic, "ai": ai_count}
