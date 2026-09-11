from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

from app.sources.base import CompanyLead, SearchSpec, SourceAdapter, SourceBlocked, SourceError


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _YandexOrganization:
    id: str | None
    name: str | None
    category: str | None
    address: str | None
    phone: str | None
    email: str | None
    site: str | None
    branches_count: int | None
    url: str | None
    raw: dict


def _parse_org(item: dict) -> _YandexOrganization:
    phones = item.get("phones") or []
    urls = item.get("urls") or []
    website = urls[0] if urls else None
    if website:
        parts = urlsplit(website)
        website = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    chain_count = (item.get("chain") or {}).get("quantityInCity")
    return _YandexOrganization(
        id=str(item.get("id")) if item.get("id") is not None else None,
        name=item.get("title"),
        category=(item.get("categories") or [{}])[0].get("name"),
        address=item.get("fullAddress") or item.get("address"),
        phone=";".join(phone.get("value", "") for phone in phones) or None,
        email=None,
        site=website,
        branches_count=chain_count if isinstance(chain_count, int) and chain_count > 0 else None,
        url=(
            f"https://yandex.ru/maps/org/{item.get('seoname')}/{item.get('id')}/"
            if item.get("id")
            else None
        ),
        raw=item,
    )


def _page_url(url: str, page: int) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if page > 1:
        query["page"] = [str(page)]
    else:
        query.pop("page", None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


class YandexMapsAdapter(SourceAdapter):
    name = "yandex_maps"

    def __init__(
        self,
        *,
        use_grid: bool = True,
        enrich_emails: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.use_grid = use_grid
        self.enrich_emails = enrich_emails
        self.transport = transport

    @staticmethod
    def _organizations_from_html(page_html: str):
        from yamaps_parser import extract_state

        state = extract_state(page_html)
        organizations = []
        for entry in state.get("stack") or []:
            results = entry.get("results") or {}
            organizations.extend(
                _parse_org(item)
                for item in results.get("items") or []
                if item.get("type") == "business"
            )
        return organizations

    def _fetch_page(self, client: httpx.Client, url: str, *, attempts: int):
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = client.get(url)
                digest = hashlib.sha256(response.content).hexdigest()[:12]
                logger.info(
                    "Yandex Maps response: status=%s bytes=%s sha256=%s attempt=%s",
                    response.status_code,
                    len(response.content),
                    digest,
                    attempt,
                )
                if response.status_code in {401, 403} or "showcaptcha" in str(response.url).casefold():
                    raise SourceBlocked("Yandex Maps returned an access-check page")
                response.raise_for_status()
                return response
            except SourceBlocked:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
        raise SourceError(f"Yandex Maps request failed after {attempts} attempts: {last_error}")

    def _collect_http(self, url: str, *, limit: int, timeout: float, attempts: int):
        from yamaps_parser import DEFAULT_HEADERS

        collected = {}
        with httpx.Client(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            for page_number in range(1, 11):
                response = self._fetch_page(client, _page_url(url, page_number), attempts=attempts)
                page_items = self._organizations_from_html(response.text)
                before = len(collected)
                for organization in page_items:
                    if organization.id:
                        collected[organization.id] = organization
                    if len(collected) >= limit:
                        break
                if len(collected) >= limit or not page_items or len(collected) == before:
                    break
        return list(collected.values())[:limit]

    def collect(self, spec: SearchSpec) -> Iterable[CompanyLead]:
        try:
            from yamaps_parser import (
                build_search_url,
                enrich_emails,
                geocode,
                search_all_browser,
                search_grid,
            )

            force_browser = bool(spec.options.get("force_browser", False))
            if force_browser:
                geo = geocode(spec.city)
                url = build_search_url(spec.query, center=geo["center"], span=geo["span"])
                use_grid = bool(spec.options.get("use_grid", self.use_grid))
                if use_grid:
                    organizations = search_grid(
                        url,
                        geo["bbox"],
                        limit=spec.limit,
                        headless=True,
                        verbose=False,
                    )
                else:
                    organizations = search_all_browser(url, limit=spec.limit, headless=True)
            else:
                timeout = float(spec.options.get("http_timeout_seconds", 25))
                attempts = max(1, min(int(spec.options.get("http_attempts", 2)), 4))
                from yamaps_parser import DEFAULT_HEADERS

                with httpx.Client(
                    headers=DEFAULT_HEADERS,
                    follow_redirects=True,
                    timeout=timeout,
                    transport=self.transport,
                ) as client:
                    geocode_error: Exception | None = None
                    for attempt in range(1, attempts + 1):
                        try:
                            geo = geocode(spec.city, client=client)
                            break
                        except (httpx.HTTPError, ValueError) as exc:
                            geocode_error = exc
                            if attempt < attempts:
                                time.sleep(0.5 * attempt)
                    else:
                        raise SourceError(f"Yandex Maps geocoding failed after {attempts} attempts: {geocode_error}")
                url = build_search_url(spec.query, center=geo["center"], span=geo["span"])
                organizations = self._collect_http(
                    url,
                    limit=spec.limit,
                    timeout=timeout,
                    attempts=attempts,
                )
                if not organizations:
                    # A second public-page shape is useful when a viewport-specific
                    # request yields no stack results. The city remains part of the
                    # query, so this does not silently broaden the requested area.
                    fallback_url = "https://yandex.ru/maps/?" + urlencode(
                        {"text": f"{spec.query} {spec.city}".strip()}
                    )
                    organizations = self._collect_http(
                        fallback_url,
                        limit=spec.limit,
                        timeout=timeout,
                        attempts=attempts,
                    )
            # LeadFlow enriches websites itself so it can retain page URL,
            # method and confidence. Upstream enrichment remains available only
            # for compatibility when explicitly requested.
            if spec.options.get("upstream_email_enrichment", False):
                enrich_emails(organizations, verbose=False)
        except SourceBlocked:
            raise
        except Exception as exc:
            if "captcha" in str(exc).casefold():
                raise SourceBlocked(str(exc)) from exc
            raise SourceError(str(exc)) from exc

        for org in organizations:
            raw = org.raw if isinstance(org, _YandexOrganization) else (asdict(org) if is_dataclass(org) else {})
            yield CompanyLead(
                source=self.name,
                source_external_id=org.id,
                source_url=org.url,
                company_name=org.name or "Unknown name",
                category=org.category or spec.category,
                city=spec.city,
                address=org.address,
                phone=org.phone,
                email=org.email,
                website=org.site,
                branches_count=getattr(org, "branches_count", None),
                raw_data=raw,
            )
