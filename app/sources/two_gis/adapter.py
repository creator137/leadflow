from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from app.sources.base import CompanyLead, SearchSpec, SourceAdapter, SourceBlocked, SourceError


logger = logging.getLogger(__name__)
_INITIAL_STATE_RE = re.compile(r"initialState\s*=\s*JSON\.parse\('(.*?)'\);", re.DOTALL)
_PUBLIC_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "accept-language": "ru-RU,ru;q=0.9",
}


def _extract_initial_state(page_html: str) -> dict[str, Any]:
    match = _INITIAL_STATE_RE.search(page_html)
    if not match:
        raise ValueError("2GIS initial state was not found")
    # The JSON is embedded in a JavaScript single-quoted string, so its
    # backslashes are escaped once for JavaScript and once for JSON.
    raw = match.group(1).replace("\\\\", "\\").replace("\\'", "'")
    return json.loads(raw)


def _profile_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = (((state.get("data") or {}).get("entity") or {}).get("profile") or {})
    return [
        value["data"]
        for value in profiles.values()
        if isinstance(value, dict) and isinstance(value.get("data"), dict)
    ]


def _first_contact(item: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for group in item.get("contact_groups") or []:
        for contact in group.get("contacts") or []:
            if contact.get("type") == kind:
                return contact
    return None


class TwoGisAdapter(SourceAdapter):
    name = "two_gis"

    def __init__(
        self,
        *,
        chrome_binary: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.chrome_binary = chrome_binary
        self.transport = transport

    @staticmethod
    def _city(city_name: str) -> tuple[str, str]:
        data_file = files("parser_2gis").joinpath("data/cities.json")
        cities = json.loads(data_file.read_text(encoding="utf-8"))
        wanted = city_name.casefold().strip()
        for city in cities:
            if city["name"].casefold() == wanted:
                return city["code"], city["domain"]
        raise SourceError(f"2GIS city is not present in upstream catalog: {city_name}")

    def _fetch(self, client: httpx.Client, url: str, *, attempts: int) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = client.get(url)
                digest = hashlib.sha256(response.content).hexdigest()[:12]
                logger.info(
                    "2GIS response: status=%s bytes=%s sha256=%s attempt=%s",
                    response.status_code,
                    len(response.content),
                    digest,
                    attempt,
                )
                if response.status_code in {401, 403}:
                    raise SourceBlocked("2GIS returned an access-check page")
                response.raise_for_status()
                return response
            except SourceBlocked:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
        raise SourceError(f"2GIS request failed after {attempts} attempts: {last_error}")

    def _collect_http(self, search_url: str, *, domain: str, city_code: str, spec: SearchSpec) -> list[dict[str, Any]]:
        timeout = float(spec.options.get("http_timeout_seconds", 25))
        attempts = max(1, min(int(spec.options.get("http_attempts", 2)), 4))
        fetch_details = bool(spec.options.get("fetch_details", True))
        delay = max(0.0, min(float(spec.options.get("delay_ms", 300)) / 1000, 2.0))
        with httpx.Client(
            headers=_PUBLIC_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            response = self._fetch(client, search_url, attempts=attempts)
            items = _profile_items(_extract_initial_state(response.text))[: spec.limit]
            if not fetch_details:
                return items
            detailed: list[dict[str, Any]] = []
            for item in items:
                item_id = str(item.get("id") or "")
                if not item_id:
                    detailed.append(item)
                    continue
                try:
                    detail_url = f"https://2gis.{domain}/{city_code}/firm/{item_id}"
                    detail_response = self._fetch(client, detail_url, attempts=attempts)
                    candidates = _profile_items(_extract_initial_state(detail_response.text))
                    exact = next(
                        (candidate for candidate in candidates if str(candidate.get("id")) == item_id),
                        None,
                    )
                    detailed.append(exact or item)
                except (SourceError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning(
                        "2GIS detail response could not be parsed for id=%s: %s",
                        item_id,
                        type(exc).__name__,
                    )
                    detailed.append(item)
                if delay:
                    time.sleep(delay)
            return detailed

    def _collect_legacy(self, search_url: str, spec: SearchSpec) -> list[dict[str, Any]]:
        executable = spec.options.get("executable") or shutil.which("parser-2gis-new")
        if not executable:
            sibling = Path(sys.executable).with_name("parser-2gis-new.exe" if os.name == "nt" else "parser-2gis-new")
            executable = str(sibling) if sibling.exists() else None
        if not executable:
            raise SourceError("parser-2gis-new executable was not found")
        with tempfile.TemporaryDirectory(prefix="leadflow-2gis-") as temp_dir:
            output = Path(temp_dir) / "result.json"
            command = [
                executable,
                "-i", search_url,
                "-o", str(output),
                "-f", "json",
                "--chrome.headless", "yes",
                "--parser.max-records", str(spec.limit),
                "--parser.delay_between_clicks", str(spec.options.get("delay_ms", 300)),
                "--writer.verbose", "no",
            ]
            chrome_binary = spec.options.get("chrome_binary") or self.chrome_binary
            if chrome_binary:
                command.extend(["--chrome.binary_path", chrome_binary])
            timeout = int(spec.options.get("timeout_seconds", 1800))
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SourceError(f"2GIS parser timed out after {timeout}s") from exc

            diagnostic = f"{result.stdout}\n{result.stderr}"
            if "captcha" in diagnostic.casefold():
                raise SourceBlocked("2GIS returned a CAPTCHA page")
            if result.returncode or not output.exists():
                tail = diagnostic[-2000:]
                raise SourceError(f"2GIS parser failed with code {result.returncode}: {tail}")
            items = json.loads(output.read_text(encoding="utf-8-sig"))
        return items

    def collect(self, spec: SearchSpec) -> Iterable[CompanyLead]:
        city_code, domain = self._city(spec.city)
        search_url = f"https://2gis.{domain}/{city_code}/search/{quote(spec.query)}"
        try:
            if spec.options.get("force_legacy_browser", False):
                items = self._collect_legacy(search_url, spec)
            else:
                items = self._collect_http(search_url, domain=domain, city_code=city_code, spec=spec)
        except SourceBlocked:
            raise
        except Exception as exc:
            raise SourceError(str(exc)) from exc

        for item in items:
            item_id = str(item.get("id") or "").split("_", 1)[0] or None
            phone = _first_contact(item, "phone")
            email = _first_contact(item, "email")
            website = _first_contact(item, "website")
            rubrics = item.get("rubrics") or []
            organization = item.get("org") or {}
            branch_count = organization.get("branch_count")
            city = next(
                (part.get("name") for part in item.get("adm_div") or [] if part.get("type") == "city"),
                spec.city,
            )
            yield CompanyLead(
                source=self.name,
                source_external_id=item_id,
                source_url=(f"https://2gis.{domain}/{city_code}/firm/{item_id}" if item_id else search_url),
                company_name=item.get("name") or (item.get("name_ex") or {}).get("primary") or "Unknown name",
                category=(rubrics[0].get("name") if rubrics else spec.category),
                city=city,
                address=item.get("address_name"),
                phone=(phone or {}).get("value") or (phone or {}).get("text"),
                email=(email or {}).get("value") or (email or {}).get("text"),
                website=(website or {}).get("url") or (website or {}).get("text"),
                branches_count=(branch_count if isinstance(branch_count, int) and branch_count > 0 else None),
                raw_data=item,
            )
