from __future__ import annotations

import re
from collections.abc import Mapping

from app.models import Company


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(value: str | None) -> str:
    return (value or "").strip().casefold()


def valid_email(value: str | None) -> str | None:
    normalized = normalize_email(value)
    return normalized if EMAIL_RE.fullmatch(normalized) else None


def resolve_recipient_email(
    company: Company, *, sheet_fields: Mapping[str, str | None] | None = None,
) -> str | None:
    """Resolve an automatic recipient without changing persisted Company fields.

    The decision-maker email always wins when it is valid. ``Company.email`` is
    retained as a final compatibility alias for old parser records where the
    general company address predates ``company_email``.
    """
    if sheet_fields is not None:
        # The selected Sheet row is authoritative for this action. Mapping is
        # semantic (not tied to column K), so duplicate "Почта" headers remain
        # safe when columns are moved.
        return (
            valid_email(sheet_fields.get("decision_maker_email"))
            or valid_email(sheet_fields.get("company_email"))
        )
    return valid_email(company.decision_maker_email) or valid_email(company.company_email) or valid_email(company.email)
