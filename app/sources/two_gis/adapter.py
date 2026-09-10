from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from app.sources.base import CompanyLead, SearchSpec, SourceAdapter, SourceBlocked, SourceError


def _first_contact(item: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for group in item.get("contact_groups") or []:
        for contact in group.get("contacts") or []:
            if contact.get("type") == kind:
                return contact
    return None


class TwoGisAdapter(SourceAdapter):
    name = "two_gis"

    def __init__(self, *, chrome_binary: str | None = None) -> None:
        self.chrome_binary = chrome_binary

    @staticmethod
    def _city(city_name: str) -> tuple[str, str]:
        data_file = files("parser_2gis").joinpath("data/cities.json")
        cities = json.loads(data_file.read_text(encoding="utf-8"))
        wanted = city_name.casefold().strip()
        for city in cities:
            if city["name"].casefold() == wanted:
                return city["code"], city["domain"]
        raise SourceError(f"2GIS city is not present in upstream catalog: {city_name}")

    def collect(self, spec: SearchSpec) -> Iterable[CompanyLead]:
        executable = spec.options.get("executable") or shutil.which("parser-2gis-new")
        if not executable:
            sibling = Path(sys.executable).with_name("parser-2gis-new.exe" if os.name == "nt" else "parser-2gis-new")
            executable = str(sibling) if sibling.exists() else None
        if not executable:
            raise SourceError("parser-2gis-new executable was not found")
        city_code, domain = self._city(spec.city)
        search_url = f"https://2gis.{domain}/{city_code}/search/{quote(spec.query)}"

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
