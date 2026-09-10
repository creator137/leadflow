from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


class SourceError(RuntimeError):
    pass


class SourceBlocked(SourceError):
    pass


@dataclass(slots=True)
class SearchSpec:
    category: str
    city: str
    keywords: list[str] = field(default_factory=list)
    limit: int = 50
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def query(self) -> str:
        return " ".join([self.category, *self.keywords]).strip()


@dataclass(slots=True)
class CompanyLead:
    source: str
    source_external_id: str | None
    source_url: str | None
    company_name: str
    category: str | None = None
    city: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    branches_count: int | None = None
    inn: str | None = None
    contact_person: str | None = None
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_data: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(ABC):
    name: str

    @abstractmethod
    def collect(self, spec: SearchSpec) -> Iterable[CompanyLead]:
        raise NotImplementedError
