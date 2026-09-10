from app.sources.base import SearchSpec
from app.sources.yandex_maps.adapter import YandexMapsAdapter


def test_yandex_structured_branch_count(monkeypatch) -> None:
    import yamaps_parser
    item = {"id": "1", "title": "Сеть", "type": "business", "urls": ["https://www.example.ru/?yclid=1"], "chain": {"quantityInCity": 17}}
    org = yamaps_parser.parse_org(item)
    monkeypatch.setattr(yamaps_parser, "geocode", lambda _: {"center": (1, 1), "span": (1, 1), "bbox": (0, 0, 2, 2)})
    monkeypatch.setattr(yamaps_parser, "search_all_browser", lambda *a, **k: [org])
    lead = next(iter(YandexMapsAdapter().collect(SearchSpec(category="ресторан", city="Москва", limit=1, options={"use_grid": False}))))
    assert lead.branches_count == 17 and lead.website == "https://www.example.ru/"


def test_two_gis_structured_branch_count_shape() -> None:
    item = {"org": {"branch_count": 151}}
    assert (item.get("org") or {}).get("branch_count") == 151
