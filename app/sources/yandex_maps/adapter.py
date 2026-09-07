from __future__ import annotations

from dataclasses import asdict
from typing import Iterable

from app.sources.base import CompanyLead, SearchSpec, SourceAdapter, SourceBlocked, SourceError


class YandexMapsAdapter(SourceAdapter):
    name = "yandex_maps"

    def __init__(self, *, use_grid: bool = True, enrich_emails: bool = False) -> None:
        self.use_grid = use_grid
        self.enrich_emails = enrich_emails

    def collect(self, spec: SearchSpec) -> Iterable[CompanyLead]:
        try:
            from yamaps_parser import (
                build_search_url,
                enrich_emails,
                geocode,
                search_all_browser,
                search_grid,
            )

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
            # LeadFlow enriches websites itself so it can retain page URL,
            # method and confidence. Upstream enrichment remains available only
            # for compatibility when explicitly requested.
            if spec.options.get("upstream_email_enrichment", False):
                enrich_emails(organizations, verbose=False)
        except Exception as exc:
            if "captcha" in str(exc).casefold():
                raise SourceBlocked(str(exc)) from exc
            raise SourceError(str(exc)) from exc

        for org in organizations:
            raw = asdict(org)
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
                raw_data=raw,
            )
