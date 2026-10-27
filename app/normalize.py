from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit


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
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return None
    # Several businesses can publish different pages on one shared host.
    # Keep their profile path in the identity used for deduplication.
    if host in {"taplink.cc", "taplink.ws", "azimuthotels.com"}:
        path = urlsplit(candidate).path.strip("/").casefold()
        return f"{host}/{path}"[:255] if path else None
    return host


def normalize_website(value: str | None) -> str | None:
    """Return a stable public website URL, never a maps/search/tracking URL."""
    if not value:
        return None
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    parts = urlsplit(candidate)
    host = (parts.hostname or "").casefold().rstrip(".")
    if not host:
        return None
    host = host.removeprefix("www.")
    blocked = ("yandex.ru", "yandex.com", "2gis.ru", "2gis.com", "google.com", "google.ru")
    if any(host == domain or host.endswith("." + domain) for domain in blocked):
        return None
    port = f":{parts.port}" if parts.port and parts.port not in {80, 443} else ""
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.casefold() if parts.scheme in {"http", "https"} else "https", host + port, path, "", ""))


def name_address_fingerprint(name: str | None, address: str | None) -> str | None:
    normalized_name = normalize_text(name)
    normalized_address = normalize_text(address)
    if not normalized_name or not normalized_address:
        return None
    raw = f"{normalized_name}|{normalized_address}".encode()
    return hashlib.sha256(raw).hexdigest()
