import html
import json

import httpx

from app.sources.base import SearchSpec
from app.sources.two_gis.adapter import TwoGisAdapter, _extract_initial_state
from app.sources.yandex_maps.adapter import YandexMapsAdapter, _page_url


def _yandex_state(state: dict) -> str:
    return f'<script class="state-view">{html.escape(json.dumps(state, ensure_ascii=False))}</script>'


def _two_gis_state(state: dict) -> str:
    payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("\\", "\\\\").replace("'", "\\'")
    return f"<script>window.initialState = JSON.parse('{payload}');</script>"


def test_yandex_uses_public_ssr_state_without_browser() -> None:
    geocode = {
        "map": {"location": {"center": [37.6, 55.7], "span": [0.4, 0.3], "bounds": [[37.4, 55.5], [37.8, 55.9]]}},
        "stack": [],
    }
    search = {
        "stack": [{"results": {"items": [{
            "type": "business",
            "id": "ya-1",
            "title": "Ресторан",
            "seoname": "restaurant",
            "fullAddress": "Москва, улица 1",
            "coordinates": [37.6, 55.7],
            "chain": {"quantityInCity": 5},
        }]}}]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = geocode if request.url.params.get("text") == "Москва" else search
        return httpx.Response(200, text=_yandex_state(body))

    adapter = YandexMapsAdapter(transport=httpx.MockTransport(handler))
    leads = list(adapter.collect(SearchSpec(category="ресторан", city="Москва", limit=1)))

    assert len(leads) == 1
    assert leads[0].source_external_id == "ya-1"
    assert leads[0].branches_count == 5


def test_yandex_page_url_keeps_viewport_parameters() -> None:
    result = _page_url("https://yandex.ru/maps/?text=x&ll=1%2C2", 2)
    assert "text=x" in result and "ll=1%2C2" in result and "page=2" in result


def test_yandex_collection_starts_from_saved_page_beyond_ten() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        requested.append(page)
        items = [
            {"type": "business", "id": f"{page}-{index}", "title": f"Компания {page}-{index}"}
            for index in range(2)
        ] if page <= 12 else []
        return httpx.Response(200, text=_yandex_state({"stack": [{"results": {"items": items}}]}))

    adapter = YandexMapsAdapter(transport=httpx.MockTransport(handler))
    leads, exhausted = adapter._collect_http(
        "https://yandex.ru/maps/?text=кейтеринг", limit=10, timeout=5,
        attempts=1, start_page=11, start_index=1,
    )

    assert requested == [11, 12, 13]
    assert [(item.id, item.search_page, item.search_index, item.search_page_size) for item in leads] == [
        ("11-1", 11, 1, 2), ("12-0", 12, 0, 2), ("12-1", 12, 1, 2),
    ]
    assert exhausted is True


def test_yandex_city_tile_uses_its_own_viewport(monkeypatch) -> None:
    requested = []
    monkeypatch.setattr("yamaps_parser.geocode", lambda *_args, **_kwargs: {
        "center": (2, 2), "span": (4, 4), "bbox": (0, 0, 4, 4),
    })

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.url.params.get("ll"), request.url.params.get("spn")))
        return httpx.Response(200, text=_yandex_state({
            "stack": [{"results": {"items": [{"type": "business", "id": "tile-card", "title": "Кейтеринг"}]}}],
        }))

    adapter = YandexMapsAdapter(transport=httpx.MockTransport(handler))
    leads = list(adapter.collect(SearchSpec(
        category="кейтеринг", city="Санкт-Петербург", limit=1,
        options={"tile_index": 4, "tile_divisions": 2},
    )))

    assert len(leads) == 1
    assert requested == [("3.000000,3.000000", "2.000000,2.000000")]


def test_two_gis_reads_modern_ssr_profiles_and_details() -> None:
    search_item = {
        "id": "7001",
        "name": "Тестовый ресторан",
        "address_name": "Тверская улица, 1",
        "adm_div": [{"type": "city", "name": "Москва"}],
        "rubrics": [{"name": "Рестораны"}],
        "org": {"branch_count": 7},
    }
    detail_item = {
        **search_item,
        "contact_groups": [{"contacts": [
            {"type": "phone", "value": "+74950000000"},
            {"type": "email", "value": "info@example.ru"},
            {"type": "website", "url": "https://example.ru"},
        ]}],
    }

    def state(item: dict) -> str:
        return _two_gis_state({"data": {"entity": {"profile": {item["id"]: {"data": item}}}}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=state(detail_item if "/firm/" in request.url.path else search_item))

    adapter = TwoGisAdapter(transport=httpx.MockTransport(handler))
    leads = list(adapter.collect(SearchSpec(category="ресторан", city="Москва", limit=1, options={"delay_ms": 0})))

    assert len(leads) == 1
    assert leads[0].source_external_id == "7001"
    assert leads[0].branches_count == 7
    assert leads[0].phone == "+74950000000"
    assert leads[0].email == "info@example.ru"
    assert leads[0].website == "https://example.ru"
    assert leads[0].source_url == "https://2gis.ru/moscow/firm/7001"


def test_two_gis_missing_city_search_returns_no_results() -> None:
    adapter = TwoGisAdapter(transport=httpx.MockTransport(
        lambda request: httpx.Response(404, text="Not Found")
    ))

    assert list(adapter.collect(SearchSpec(category="кейтеринг", city="Москва", limit=10))) == []


def test_two_gis_initial_state_does_not_require_legacy_stat_links() -> None:
    state = {"data": {"entity": {"profile": {"1": {"data": {"id": "1", "name": "Организация"}}}}}}
    assert _extract_initial_state(_two_gis_state(state)) == state
