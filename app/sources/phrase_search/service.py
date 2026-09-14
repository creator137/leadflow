from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote_plus, urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CompanyDirection, CompanySourceRecord, Direction, PhraseSearchResult, PhraseSearchRun
from app.services.company_enrichment import crawl_website
from app.services.deepseek import DeepSeekClient
from app.services.dedup import upsert_lead
from app.services.google_sheets import active_sheets_config, sync_companies
from app.sources.base import CompanyLead, SourceError

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+7|8)[\s(.-]*\d{3}[\s).-]*\d{3}[\s.-]*\d{2}[\s.-]*\d{2}")
EXCLUDED_HOSTS = {"bing.com", "www.bing.com", "duckduckgo.com", "www.duckduckgo.com",
                  "search.brave.com", "cdn.search.brave.com", "imgs.search.brave.com", "tiles.search.brave.com"}


class FreeSearchProvider:
    """Discover only URLs actually returned by free public search pages."""

    def __init__(self) -> None:
        self.headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7"}

    @staticmethod
    def _clean_url(value: str) -> str | None:
        if value.startswith("//duckduckgo.com/l/"):
            value = "https:" + value
        parsed = urlsplit(value)
        if parsed.hostname in {"duckduckgo.com", "www.duckduckgo.com"} and parsed.path.startswith("/l/"):
            value = parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.casefold() in EXCLUDED_HOSTS:
            return None
        return value.split("#", 1)[0]

    def search(self, phrase: str, limit: int) -> list[str]:
        query, found = quote_plus(phrase), []
        with httpx.Client(headers=self.headers, follow_redirects=True, timeout=20) as client:
            try:
                response = client.get(f"https://search.brave.com/search?q={query}&source=web")
                response.raise_for_status()
                for link in BeautifulSoup(response.text, "html.parser").select("a[href]"):
                    url = self._clean_url(str(link.get("href") or ""))
                    if url and url not in found and not urlsplit(url).path.casefold().endswith((".css", ".js", ".png", ".svg", ".ico", ".woff2")):
                        found.append(url)
            except httpx.HTTPError:
                pass
            if len(found) >= limit:
                return found[:limit]
            try:
                response = client.get(f"https://www.bing.com/search?format=rss&q={query}")
                response.raise_for_status()
                for link in BeautifulSoup(response.text, "xml").select("item > link"):
                    url = self._clean_url(link.get_text(strip=True))
                    if url and url not in found:
                        found.append(url)
            except httpx.HTTPError:
                pass
            if len(found) < limit:
                try:
                    response = client.get(f"https://html.duckduckgo.com/html/?q={query}")
                    response.raise_for_status()
                    for link in BeautifulSoup(response.text, "html.parser").select("a.result__a[href]"):
                        url = self._clean_url(str(link.get("href") or ""))
                        if url and url not in found:
                            found.append(url)
                except httpx.HTTPError:
                    pass
        if not found:
            raise SourceError("Бесплатный поиск временно не вернул публичные страницы.")
        return found[:limit]


def _organization_metadata(html: str | BeautifulSoup) -> dict[str, str]:
    soup, pending = (html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")), []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            pending.append(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue
    while pending:
        value = pending.pop(0)
        if isinstance(value, list):
            pending.extend(value)
            continue
        if not isinstance(value, dict):
            continue
        pending.extend(child for child in value.values() if isinstance(child, (dict, list)))
        kinds = value.get("@type", [])
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not value.get("name") or not any(str(kind) in {"Organization", "LocalBusiness", "Corporation"} for kind in kinds):
            continue
        result = {"name": str(value["name"]).strip()}
        address = value.get("address")
        if isinstance(address, dict):
            result["address"] = ", ".join(str(address.get(k, "")).strip() for k in ("postalCode", "addressRegion", "addressLocality", "streetAddress") if address.get(k))
            result["city"] = str(address.get("addressLocality") or "").strip()
        for source, target in (("telephone", "phone"), ("email", "email"), ("url", "website")):
            if value.get(source):
                result[target] = str(value[source]).strip()
        return result
    return {}


class PhraseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relevant: bool
    company_name: str | None = None
    email: str | None = None
    phone: str | None = None
    evidence_text: str | None = Field(default=None, max_length=500)


DECISION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["relevant", "company_name", "email", "phone", "evidence_text"], "properties": {"relevant": {"type": "boolean"}, "company_name": {"type": ["string", "null"]}, "email": {"type": ["string", "null"]}, "phone": {"type": ["string", "null"]}, "evidence_text": {"type": ["string", "null"], "maxLength": 500}}}


def _page_evidence(text: str, phrase: str) -> str | None:
    tokens = [token for token in re.findall(r"[\w-]+", phrase.casefold()) if len(token) > 2]
    for part in re.split(r"(?<=[.!?])\s+", text):
        if sum(token in part.casefold() for token in tokens) >= max(1, min(2, len(tokens))):
            return part[:500]
    return None


def _ai_decision(session: Session, settings: Settings, run: PhraseSearchRun, url: str, text: str) -> PhraseDecision:
    prompt = json.dumps({"phrase": run.phrase, "city": run.city, "source_url": url, "page_text": text[:5000]}, ensure_ascii=False, separators=(",", ":"))
    content_hash = hashlib.sha256((url + "\n" + text).encode()).hexdigest()
    result = DeepSeekClient(session, settings).generate(company_id=None, operation="phrase_search", content_hash=content_hash, missing_fields=["relevance", "company_name", "email", "phone"], prompt_version="phrase-search-v1", instructions="Определи, описывает ли публичная страница реальную организацию, соответствующую фразе и городу. Извлекай только явно указанные значения. Не придумывай URL, название или контакты. evidence_text должен быть точной короткой цитатой из page_text; без подтверждения relevant=false.", input_text=prompt, response_model=PhraseDecision, schema=DECISION_SCHEMA, max_output_tokens=220, use_cache=True)
    decision = result.data
    if not decision.evidence_text or decision.evidence_text.casefold() not in text.casefold():
        decision.relevant = False
    return decision


def execute_phrase_search(session: Session, run: PhraseSearchRun, settings: Settings, limit: int = 20) -> PhraseSearchRun:
    run.status = "running"
    session.commit()
    try:
        query = " ".join(part for part in (run.phrase, run.city, run.region) if part)
        urls = FreeSearchProvider().search(query, limit)
        run.urls_discovered = len(urls)
        session.commit()
        direction = session.get(Direction, run.direction_id) if run.direction_id else None
        for url in urls:
            try:
                snapshot = crawl_website(url, max_pages=2)
                if not snapshot.pages:
                    raise ValueError("Публичную страницу не удалось прочитать.")
                page, method = snapshot.pages[0], "deterministic"
                metadata = _organization_metadata(page.html)
                evidence_text = _page_evidence(page.text, run.phrase)
                decision = None
                if run.use_ai and (not metadata.get("name") or not evidence_text):
                    decision = _ai_decision(session, settings, run, page.url, page.text)
                    method, evidence_text = "ai", decision.evidence_text
                if not evidence_text or (decision and not decision.relevant):
                    session.add(PhraseSearchResult(run_id=run.id, phrase=run.phrase, source_url=page.url, extraction_method=method, status="irrelevant", evidence=[]))
                    session.commit()
                    continue
                host = urlsplit(page.url).hostname or ""
                company_name = metadata.get("name") or (decision.company_name if decision else None) or host.removeprefix("www.")
                emails, phones = EMAIL_RE.findall(page.text), PHONE_RE.findall(page.text)
                email = metadata.get("email") or (decision.email if decision else None) or (emails[0] if emails else None)
                phone = metadata.get("phone") or (decision.phone if decision else None) or (phones[0] if phones else None)
                website = metadata.get("website") or f"{urlsplit(page.url).scheme}://{host}"
                lead = CompanyLead(source="phrase_search", source_external_id=page.url, source_url=page.url, company_name=company_name[:500], category=direction.name if direction else None, city=metadata.get("city") or run.city, address=metadata.get("address"), website=website, phone=phone, email=email, raw_data={"phrase": run.phrase, "evidence": evidence_text, "source_url": page.url})
                upsert = upsert_lead(session, lead)
                linked = direction and session.scalar(select(CompanyDirection.id).where(
                    CompanyDirection.company_id == upsert.company.id, CompanyDirection.direction_id == direction.id))
                if direction and not linked:
                    session.add(CompanyDirection(company_id=upsert.company.id, direction_id=direction.id))
                exists = session.scalar(select(CompanySourceRecord.id).where(CompanySourceRecord.company_id == upsert.company.id, CompanySourceRecord.source == "phrase_search", CompanySourceRecord.source_url == page.url))
                if not exists:
                    session.add(CompanySourceRecord(company_id=upsert.company.id, source="phrase_search", source_external_id=page.url, source_url=page.url, raw_data=lead.raw_data))
                status = "added" if upsert.inserted else "duplicate"
                run.new_count += int(upsert.inserted)
                run.duplicate_count += int(not upsert.inserted)
                run.result_count += 1
                session.add(PhraseSearchResult(run_id=run.id, company_id=upsert.company.id, phrase=run.phrase, source_url=page.url, company_name=upsert.company.company_name, website=upsert.company.website, email=upsert.company.company_email, phone=upsert.company.company_phone, extraction_method=method, status=status, evidence=[{"field": "relevance", "value": run.phrase, "source_url": page.url, "evidence_text": evidence_text}]))
                session.commit()
            except Exception:
                session.rollback()
                session.add(PhraseSearchResult(run_id=run.id, phrase=run.phrase, source_url=url, status="error", error="Страницу не удалось обработать."))
                session.commit()
        config = active_sheets_config(session, settings)
        if config and run.result_count:
            sync_companies(session, config)
        run = session.get(PhraseSearchRun, run.id)
        run.status = "completed"
    except Exception as exc:
        session.rollback()
        run = session.get(PhraseSearchRun, run.id)
        run.status = "failed"
        run.error = str(exc) if re.search(r"[А-Яа-яЁё]", str(exc)) else "Бесплатный поиск временно недоступен."
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run
