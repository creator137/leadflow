"""A deterministic local city catalog used by regional searches.

The parser adapters search a concrete city, not a whole administrative subject.
Keeping the expansion on our side makes a regional run transparent, repeatable
and lets its single new-company limit and deduplication work as intended.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "russian_region_cities.json"


@lru_cache(maxsize=1)
def _catalog() -> dict[str, tuple[str, ...]]:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {str(region): tuple(str(city) for city in cities) for region, cities in data.items()}


def cities_for_region(region: str) -> tuple[str, ...]:
    return _catalog().get(region.strip(), ())


def available_regions() -> list[dict[str, object]]:
    return [
        {"name": name, "cities_count": len(cities)}
        for name, cities in sorted(_catalog().items())
    ]
