from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit


LEGAL_FORMS = re.compile(r"\b(?:ооо|оао|пао|ао|зао|ип)\b", re.IGNORECASE)
NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize_text(value: str | None) -> str | None:
    if not value:
        return None
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = LEGAL_FORMS.sub(" ", value)
    value = NON_WORD.sub(" ", value)
    return " ".join(value.split()) or None


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value.split(";")[0])
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    return digits if len(digits) >= 10 else None


def website_domain(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    host = (urlsplit(candidate).hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else (host or None)


def name_address_fingerprint(name: str | None, address: str | None) -> str | None:
    normalized_name = normalize_text(name)
    normalized_address = normalize_text(address)
    if not normalized_name or not normalized_address:
        return None
    raw = f"{normalized_name}|{normalized_address}".encode()
    return hashlib.sha256(raw).hexdigest()

