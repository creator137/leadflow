from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CONTACT_WORDS = ("contact", "contacts", "kontakty", "контакт", "about", "о-компании", "реквизит")
BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js"}
BAD_DOMAINS = {"example.com", "sentry.io", "wixpress.com", "schema.org", "w3.org"}


@dataclass(frozen=True, slots=True)
class DiscoveredEmail:
    email: str
    page_url: str
    confidence: float
    method: str


def _emails(soup: BeautifulSoup, page_url: str) -> list[DiscoveredEmail]:
    mailtos = {
        unquote(link.get("href", "")[7:]).split("?", 1)[0].strip()
        for link in soup.select('a[href^="mailto:"]')
    }
    text_matches = set(EMAIL_RE.findall(soup.get_text(" ")))
    is_contact = any(word in urlsplit(page_url).path.casefold() for word in CONTACT_WORDS)
    results = []
    for candidate in sorted(mailtos | text_matches):
        normalized = candidate.strip(".,;: ").casefold()
        domain = normalized.rsplit("@", 1)[-1]
        tld = domain.rsplit(".", 1)[-1]
        if "@" not in normalized or tld in BAD_TLDS or domain in BAD_DOMAINS:
            continue
        method = "mailto" if candidate in mailtos else "visible_text"
        confidence = 1.0 if method == "mailto" else (0.85 if is_contact else 0.65)
        results.append(DiscoveredEmail(normalized, page_url, confidence, method))
    return results


def discover_emails(site_url: str, *, max_pages: int = 6) -> list[DiscoveredEmail]:
    start = site_url if "://" in site_url else "https://" + site_url
    origin = urlsplit(start).netloc.casefold()
    queue = [start]
    visited: set[str] = set()
    found: dict[str, DiscoveredEmail] = {}
    headers = {"User-Agent": "LeadFlow/0.1 public-contact-indexer", "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=12) as client:
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
            soup = BeautifulSoup(response.text, "html.parser")
            for item in _emails(soup, str(response.url)):
                previous = found.get(item.email)
                if not previous or item.confidence > previous.confidence:
                    found[item.email] = item
            for link in soup.select("a[href]"):
                href = link.get("href", "")
                label = f"{href} {link.get_text(' ')}".casefold()
                target = urljoin(str(response.url), href).split("#", 1)[0]
                if urlsplit(target).netloc.casefold() == origin and any(word in label for word in CONTACT_WORDS):
                    if target not in visited and target not in queue:
                        queue.append(target)
    return sorted(found.values(), key=lambda item: item.confidence, reverse=True)
