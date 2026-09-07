from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIConfig, PhraseSearchRun
from app.services.dedup import upsert_lead
from app.services.secrets import decrypt_secret
from app.sources.base import CompanyLead, SourceError

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+7|8)[\s()\-\d]{10,18}")


class SearchProvider(Protocol):
    def search(self, phrase: str, limit: int) -> list[str]: ...


class SerperProvider:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def search(self, phrase: str, limit: int) -> list[str]:
        response = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": f'"{phrase}"', "num": min(limit, 100)},
            timeout=30,
        )
        response.raise_for_status()
        return [item["link"] for item in response.json().get("organic", []) if item.get("link")]


def _provider(settings: Settings) -> SearchProvider:
    if settings.search_provider == "serper" and settings.serper_api_key:
        return SerperProvider(settings.serper_api_key)
    raise SourceError("No configured phrase search provider")


def _organization_metadata(soup: BeautifulSoup) -> dict[str, str]:
    candidates: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            item_type = value.get("@type", "")
            types = item_type if isinstance(item_type, list) else [item_type]
            if value.get("name") and any(
                isinstance(kind, str) and ("organization" in kind.casefold() or "business" in kind.casefold())
                for kind in types
            ):
                candidates.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (json.JSONDecodeError, TypeError):
            continue
    if not candidates:
        return {}
    item = candidates[0]
    address = item.get("address")
    result = {"name": str(item["name"]).strip()}
    if isinstance(address, dict):
        parts = [address.get(key) for key in ("postalCode", "addressRegion", "addressLocality", "streetAddress")]
        result["address"] = ", ".join(str(part).strip() for part in parts if part)
        if address.get("addressLocality"):
            result["city"] = str(address["addressLocality"]).strip()
    elif isinstance(address, str):
        result["address"] = address.strip()
    for source, target in (("telephone", "phone"), ("email", "email"), ("url", "website")):
        if item.get(source):
            result[target] = str(item[source]).strip()
    return result


def _semantic_match(session: Session, phrase: str, text: str) -> tuple[bool, float]:
    config = session.query(AIConfig).filter(AIConfig.active.is_(True)).order_by(AIConfig.updated_at.desc()).first()
    if not config:
        return False, 0.0
    try:
        response = httpx.post(
            config.api_base.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {decrypt_secret(config.api_key_encrypted)}"},
            json={
                "model": config.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{
                    "role": "user",
                    "content": (
                        "Determine whether the page describes the same concrete event, service, or subject as the "
                        "query. Return JSON {\"match\": boolean, \"score\": number from 0 to 1}.\n"
                        f"Query: {phrase}\nPage: {text[:8000]}"
                    ),
                }],
            },
            timeout=60,
        )
        response.raise_for_status()
        answer = json.loads(response.json()["choices"][0]["message"]["content"])
        score = float(answer.get("score", 0))
        return bool(answer.get("match")) and score >= 0.75, score
    except Exception:
        return False, 0.0


def execute_phrase_search(session: Session, run: PhraseSearchRun, settings: Settings, limit: int = 20) -> PhraseSearchRun:
    run.status = "running"
    session.commit()
    found = 0
    try:
        urls = _provider(settings).search(run.phrase, limit)
        headers = {"User-Agent": "LeadFlow/0.1 public-contact-indexer"}
        with httpx.Client(headers=headers, follow_redirects=True, timeout=15) as client:
            for url in urls:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    if "text/html" not in response.headers.get("content-type", ""):
                        continue
                except Exception:
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                metadata = _organization_metadata(soup)
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                text = " ".join(soup.get_text(" ").split())
                exact = run.phrase.casefold() in text.casefold()
                phrase_tokens = set(re.findall(r"\w+", run.phrase.casefold()))
                page_tokens = set(re.findall(r"\w+", text.casefold()))
                token_coverage = len(phrase_tokens & page_tokens) / max(1, len(phrase_tokens))
                score = max(
                    SequenceMatcher(None, run.phrase.casefold(), text[:5000].casefold()).quick_ratio(),
                    token_coverage,
                )
                semantic = False
                if not exact and score < 0.75:
                    semantic, semantic_score = _semantic_match(session, run.phrase, text)
                    score = max(score, semantic_score)
                    if not semantic:
                        continue
                site_name = soup.select_one('meta[property="og:site_name"]')
                title = metadata.get("name") or (site_name.get("content", "").strip() if site_name else "")
                title = title or (soup.title.string.strip() if soup.title and soup.title.string else urlsplit(url).hostname or "Unknown")
                emails = [e.casefold() for e in EMAIL_RE.findall(text)]
                phones = PHONE_RE.findall(text)
                lead = CompanyLead(
                    source="phrase_search",
                    source_external_id=url,
                    source_url=url,
                    company_name=title[:500],
                    website=metadata.get("website") or f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
                    phone=metadata.get("phone") or (phones[0] if phones else None),
                    email=metadata.get("email") or (emails[0] if emails else None),
                    city=metadata.get("city"),
                    address=metadata.get("address"),
                    raw_data={"match_url": url, "exact_match": exact, "semantic_match": semantic, "similarity": score},
                )
                if upsert_lead(session, lead).inserted:
                    found += 1
                session.commit()
        run.status = "completed"
        run.result_count = found
    except Exception as exc:
        session.rollback()
        run = session.get(PhraseSearchRun, run.id)
        run.status = "failed"
        run.error = str(exc)
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run
