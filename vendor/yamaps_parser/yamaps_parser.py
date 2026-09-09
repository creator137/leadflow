from __future__ import annotations

import html as html_module
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit, urlencode, urljoin

import httpx

STATE_RE = re.compile(r'<script[^>]*class="state-view"[^>]*>(.*?)</script>', re.S)

DEFAULT_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    ),
    "accept-language": "ru,en;q=0.9",
}


@dataclass
class Organization:
    id: str | None
    name: str | None
    category: str | None
    address: str | None
    lon: float | None
    lat: float | None
    phone: str | None
    site: str | None
    social: str | None
    hours: str | None
    rating: float | None
    reviews: int | None
    url: str | None
    email: str | None = None


def _strip_query(url: str) -> str:
    """Убирает GET-параметры (напр. ?yclid=...) из ссылки на сайт организации."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def parse_org(item: dict[str, Any]) -> Organization:
    rd = item.get("ratingData") or {}
    coords = item.get("coordinates") or [None, None]  # порядок [lon, lat]!
    phones = item.get("phones") or []
    socials = item.get("socialLinks") or []
    urls = item.get("urls") or []
    return Organization(
        id=item.get("id"),
        name=item.get("title"),
        category=(item.get("categories") or [{}])[0].get("name"),
        address=item.get("fullAddress") or item.get("address"),
        lon=coords[0],
        lat=coords[1],
        phone=";".join(p.get("value", "") for p in phones) or None,
        site=_strip_query(urls[0]) if urls else None,
        social=";".join(s.get("href", "") for s in socials) or None,
        hours=item.get("workingTimeText"),
        rating=rd.get("ratingValue"),
        reviews=rd.get("reviewCount"),
        url=(f"https://yandex.ru/maps/org/{item.get('seoname')}/{item.get('id')}/"
             if item.get("id") else None),
    )


def orgs_from_results(results: dict[str, Any]) -> list[Organization]:
    return [parse_org(it) for it in results.get("items", []) if it.get("type") == "business"]


def extract_state(page_html: str) -> dict[str, Any]:
    """Загрузка state-view со страницы"""
    m = STATE_RE.search(page_html)
    if not m:
        raise ValueError("Блок state-view не найден — изменилась разметка или пришла капча.")
    return json.loads(html_module.unescape(m.group(1).strip()))


def geocode(place: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    """
    Определяет центр/масштаб/bbox региона по названию (город, район, страна — кириллицей).
    Возвращает {"center": (lon, lat), "span": (lon, lat), "bbox": (min_lon, min_lat, max_lon, max_lat)}.
    """
    cl, own = _client(client)
    try:
        r = cl.get("https://yandex.ru/maps/", params={"text": place})
        r.raise_for_status()
        state = extract_state(r.text)
    finally:
        if own:
            cl.close()

    loc = (state.get("map") or {}).get("location") or {}
    if not loc.get("center") or not loc.get("bounds"):
        raise ValueError(f"не удалось определить границы для {place!r} — "
                          f"уточни название (например, добавь регион/страну)")

    (min_lon, min_lat), (max_lon, max_lat) = loc["bounds"]
    return {
        "center": tuple(loc["center"]),
        "span": tuple(loc["span"]),
        "bbox": (min_lon, min_lat, max_lon, max_lat),
    }


def build_search_url(query: str, *, center: tuple[float, float], span: tuple[float, float]) -> str:
    """Собирает URL поиска Яндекс.Карт вокруг заданного центра/масштаба."""
    lon, lat = center
    spn_lon, spn_lat = span
    q = urlencode({
        "text": query,
        "ll": f"{lon:.6f},{lat:.6f}",
        "spn": f"{spn_lon:.6f},{spn_lat:.6f}",
    })
    return f"https://yandex.ru/maps/?{q}"


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=20), True


_SCROLL_CONTAINERS = (
    ".scroll__container",
    ".search-list-view__list",
    "[class*='search-list-view']",
    ".sidebar-view__panel",
)

# JS: инкрементальный скролл контейнера + событие scroll (триггерит ленивую подгрузку).
_SCROLL_JS = """(sel) => {
    const e = document.querySelector(sel);
    if (!e) return null;
    e.scrollTop = Math.min(e.scrollTop + e.clientHeight * 0.9, e.scrollHeight);
    e.dispatchEvent(new Event('scroll', {bubbles: true}));
    return {top: Math.round(e.scrollTop), sh: e.scrollHeight, ch: e.clientHeight};
}"""

# Кандидаты селектора карточки организации в списке.
_CARD_SELECTORS = (
    "[class*='search-snippet-view']",
    "[class*='search-business-snippet']",
    "li[class*='search-snippet']",
    "[class*='search-list-view'] a[href*='/org/']",
)


def _active_container(page) -> str | None:
    for sel in _SCROLL_CONTAINERS:
        try:
            if page.query_selector(sel):
                return sel
        except Exception:
            continue
    return None


def _scroll_step(page, container: str | None) -> dict | None:
    """Один инкрементальный шаг прокрутки. Возвращает метрики скролла контейнера."""
    metrics = None
    if container:
        try:
            metrics = page.evaluate(_SCROLL_JS, container)
        except Exception:
            metrics = None
    # подстраховка: колесо мыши над панелью + подтяжка последней карточки
    try:
        page.mouse.move(320, 450)
        page.mouse.wheel(0, 3000)
    except Exception:
        pass
    for sel in _CARD_SELECTORS:
        try:
            cards = page.query_selector_all(sel)
            if cards:
                cards[-1].scroll_into_view_if_needed(timeout=1500)
                break
        except Exception:
            continue
    return metrics


def _dump_scrollables(page) -> None:
    """Диагностика: печатает реально скроллируемые элементы (когда прокрутка не идёт)."""
    try:
        info = page.evaluate(
            """() => [...document.querySelectorAll('*')]
                .filter(e => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 150)
                .map(e => ({cls: (e.className||'').toString().slice(0,70),
                            ch: e.clientHeight, sh: e.scrollHeight}))
                .sort((a,b) => b.sh - a.sh).slice(0, 8)"""
        )
        import sys
        print("[debug] скроллируемые контейнеры на странице:", file=sys.stderr)
        for it in info:
            print(f"   sh={it['sh']:>6} ch={it['ch']:>4}  .{it['cls']}", file=sys.stderr)
    except Exception:
        pass


def debug_scrollables(url: str, *, headless: bool = False) -> None:
    """Отдельный прогон: открыть страницу и вывести скроллируемые контейнеры."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_context(locale="ru-RU").new_page()
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _dump_scrollables(page)
        browser.close()


def _scrape_page(
    page,
    url: str,
    *,
    max_scrolls: int = 80,
    settle_ms: int = 1200,
    stagnant_limit: int = 3,
    goto_timeout: int = 45000,
    limit: int | None = None,
) -> list[Organization]:
    """
    Открывает url на уже созданной playwright-странице, скроллит список и
    перехватывает подписанные ответы /maps/api/search. Вынесено отдельно от
    search_all_browser, чтобы вызывающий код (см. search_grid) мог переиспользовать
    один браузер/контекст на много URL подряд, не перезапуская Chromium на каждый.

    limit — прекратить скроллинг, как только собрано столько организаций.
    """
    collected: dict[str, Organization] = {}
    total_holder: dict[str, int] = {"total": 0}

    def on_response(response) -> None:
        if "/maps/api/search" not in response.url:
            return
        try:
            data = response.json().get("data", {})
        except Exception:
            return
        if not total_holder["total"]:
            total_holder["total"] = data.get("totalResultCount") or 0
        for org in orgs_from_results(data):
            if org.id:
                collected[org.id] = org

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout)

        # Страница 1 — из SSR-стейта самого документа (на случай, если её не было в XHR).
        try:
            state = extract_state(page.content())
            results = state["stack"][0]["results"]
            total_holder["total"] = total_holder["total"] or (results.get("totalResultCount") or 0)
            for org in orgs_from_results(results):
                if org.id:
                    collected[org.id] = org
        except Exception:
            pass

        page.wait_for_timeout(settle_ms)

        container = _active_container(page)
        if not container:
            _dump_scrollables(page)

        stagnant = 0
        last_sh = 0
        for _ in range(max_scrolls):
            total = total_holder["total"]
            if total and len(collected) >= total:
                break
            if limit is not None and len(collected) >= limit:
                break

            before = len(collected)
            metrics = _scroll_step(page, container)
            page.wait_for_timeout(settle_ms)

            grew = len(collected) > before
            sh_grew = bool(metrics and metrics["sh"] > last_sh)
            at_bottom = bool(metrics and metrics["top"] + metrics["ch"] >= metrics["sh"] - 4)
            if metrics:
                last_sh = metrics["sh"]

            if grew or sh_grew:
                stagnant = 0
            else:
                stagnant += 1
                if stagnant == 1 and not container:
                    _dump_scrollables(page)
                # застой + мы уже у дна и контент не растёт → выход
                if stagnant >= stagnant_limit and at_bottom:
                    break
                if stagnant >= stagnant_limit + 3:      # жёсткий предохранитель
                    break
    finally:
        page.remove_listener("response", on_response)

    result = list(collected.values())
    if limit is not None and len(result) > limit:
        result = result[:limit]
    return result


def search_all_browser(
    url: str,
    *,
    headless: bool = True,
    max_scrolls: int = 80,
    settle_ms: int = 1200,
    stagnant_limit: int = 3,
    limit: int | None = None,
) -> list[Organization]:
    """
    Полная выдача через Playwright: перехват подписанных ответов /maps/api/search.
    Подпись `s` не вычисляется — её проставляет родной JS страницы.

    url — обычная ссылка поиска Яндекс.Карт.
    stagnant_limit — сколько скроллов подряд без новых результатов считать концом списка.
    limit — остановиться, как только собрано столько организаций (по умолчанию — все).
    """
    from playwright.sync_api import sync_playwright  # локальный импорт: зависимость опциональна

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=DEFAULT_HEADERS["user-agent"],
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        try:
            return _scrape_page(
                page, url,
                max_scrolls=max_scrolls, settle_ms=settle_ms, stagnant_limit=stagnant_limit,
                limit=limit,
            )
        finally:
            browser.close()


def _set_viewport(url: str, lon: float, lat: float, spn_lon: float, spn_lat: float) -> str:
    """Подставляет в URL поиска свои ll/spn, убирая z (конфликтует с spn)."""
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    q.pop("z", None)
    q["ll"] = [f"{lon:.6f},{lat:.6f}"]
    q["spn"] = [f"{spn_lon:.6f},{spn_lat:.6f}"]
    new_query = urlencode({k: v[0] for k, v in q.items()})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def search_grid(
    url: str,
    bbox: tuple[float, float, float, float],
    *,
    cell: tuple[float, float] = (0.03, 0.02),
    min_cell: tuple[float, float] = (0.003, 0.002),
    split_threshold: int = 90,
    delay: float = 0.5,
    retries: int = 3,
    limit: int | None = None,
    verbose: bool = True,
    headless: bool = True,
    **page_kwargs: Any,
) -> list[Organization]:
    """
    Полный обход города/района сеткой viewport'ов, минуя лимит одного запроса.
    Один Chromium (Playwright) на весь обход, страница переоткрывается на каждую ячейку.

    bbox — (min_lon, min_lat, max_lon, max_lat) охватываемой области.
    cell — стартовый размер ячейки в градусах (ширина, высота) = будущий spn.
    min_cell — ячейка мельче этого не дробится (чтобы избежать рекурсии).
    split_threshold — если ячейка вернула столько организаций и больше, она
        считается "обрезанной" и делится на 4 более мелкие.
    retries — сколько раз повторить ячейку при сетевой ошибке/таймауте, прежде
        чем пропустить её (не роняя весь обход).
    limit — остановить весь обход, как только суммарно собрано столько организаций
        (по умолчанию — все, сколько найдётся).
    page_kwargs — доп. аргументы для _scrape_page (max_scrolls, settle_ms, stagnant_limit).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    collected: dict[str, Organization] = {}
    failed_cells: list[tuple[float, float, float, float]] = []

    def limit_hit() -> bool:
        return limit is not None and len(collected) >= limit

    def run_cell(fetch_one, c_lon: float, c_lat: float, span_lon: float, span_lat: float,
                 remaining: int | None):
        cell_url = _set_viewport(url, c_lon, c_lat, span_lon, span_lat)
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return fetch_one(cell_url, remaining)
            except Exception as exc:
                last_exc = exc
                if verbose:
                    print(f"[grid] ll={c_lon:.5f},{c_lat:.5f} попытка {attempt}/{retries} "
                          f"не удалась: {exc!r}", file=sys.stderr)
                time.sleep(delay * attempt)
        if verbose:
            print(f"[grid] ll={c_lon:.5f},{c_lat:.5f} пропущена после {retries} попыток: "
                  f"{last_exc!r}", file=sys.stderr)
        failed_cells.append((c_lon, c_lat, span_lon, span_lat))
        return None

    def process(fetch_one, c_lon: float, c_lat: float, span_lon: float, span_lat: float) -> None:
        if limit_hit():
            return
        remaining = None if limit is None else limit - len(collected)
        orgs = run_cell(fetch_one, c_lon, c_lat, span_lon, span_lat, remaining)
        if orgs is None:
            return
        if verbose:
            print(f"[grid] ll={c_lon:.5f},{c_lat:.5f} spn={span_lon:.5f},{span_lat:.5f} "
                  f"-> {len(orgs)}", file=sys.stderr)
        can_split = span_lon > min_cell[0] * 2 and span_lat > min_cell[1] * 2
        if len(orgs) >= split_threshold and can_split:
            half_lon, half_lat = span_lon / 2, span_lat / 2
            for dx in (-0.25, 0.25):
                for dy in (-0.25, 0.25):
                    if limit_hit():
                        return
                    time.sleep(delay)
                    process(fetch_one, c_lon + dx * span_lon, c_lat + dy * span_lat, half_lon, half_lat)
            return
        for o in orgs:
            if o.id:
                collected[o.id] = o

    def walk(fetch_one) -> None:
        step_lon, step_lat = cell
        lon = min_lon
        while lon < max_lon:
            if limit_hit():
                break
            lat = min_lat
            while lat < max_lat:
                if limit_hit():
                    break
                process(fetch_one, lon + step_lon / 2, lat + step_lat / 2, step_lon, step_lat)
                time.sleep(delay)
                lat += step_lat
            lon += step_lon

    from playwright.sync_api import sync_playwright  # локальный импорт: зависимость опциональна

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=DEFAULT_HEADERS["user-agent"],
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
        )

        def fetch_one(cell_url: str, remaining: int | None) -> list[Organization]:
            # Одна страница на браузер, но одна на ячейку: свежий DOM/список
            # откликов, без риска, что XHR от прошлой ячейки затесались в счёт.
            page = context.new_page()
            try:
                return _scrape_page(page, cell_url, limit=remaining, **page_kwargs)
            finally:
                page.close()

        walk(fetch_one)
        browser.close()

    if limit is not None and len(collected) > limit:
        # последняя обработанная ячейка могла принести чуть больше, чем нужно
        collected = dict(list(collected.items())[:limit])

    if failed_cells and verbose:
        print(f"[grid] не опрошено {len(failed_cells)} ячеек (после {retries} попыток каждая) — "
              f"данные по ним отсутствуют в результате", file=sys.stderr)

    return list(collected.values())


_MAILTO_RE = re.compile(r'mailto:([^"\'?&<>\s]+)', re.I)
_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

# Расширения статики и служебные домены, которые регекс путает с почтой
# (напр. "logo@2x.png" в srcset) или которые тянутся из шаблонов/аналитики сайтов.
_EMAIL_BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js"}
_EMAIL_BAD_DOMAINS = {"example.com", "domain.com", "yourdomain.com", "sentry.io",
                       "wixpress.com", "schema.org", "w3.org"}

_EMAIL_CONTACT_PATHS = ("/contacts", "/contact", "/kontakty", "/o-kompanii", "/about")


def _extract_emails(html_text: str) -> list[str]:
    """Ищет email в mailto-ссылках и в тексте страницы, отбрасывая явный мусор."""
    candidates = [unquote(m) for m in _MAILTO_RE.findall(html_text)]
    candidates += _EMAIL_RE.findall(html_text)

    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        addr = raw.strip().strip(".,;:")
        low = addr.lower()
        if "@" not in low:
            continue
        domain = low.rsplit("@", 1)[-1]
        tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
        if tld in _EMAIL_BAD_TLDS or domain in _EMAIL_BAD_DOMAINS:
            continue
        if low in seen:
            continue
        seen.add(low)
        out.append(addr)
    return out


def fetch_site_email(site_url: str, *, client: httpx.Client) -> str | None:
    """Заходит на сайт (и, если не нашлось, на типовую страницу контактов) за email."""
    try:
        r = client.get(site_url)
        r.raise_for_status()
    except Exception:
        return None

    emails = _extract_emails(r.text)
    if emails:
        return emails[0]

    for path in _EMAIL_CONTACT_PATHS:
        try:
            rc = client.get(urljoin(str(r.url), path))
            rc.raise_for_status()
        except Exception:
            continue
        emails = _extract_emails(rc.text)
        if emails:
            return emails[0]

    return None


def enrich_emails(
    orgs: list[Organization],
    *,
    delay: float = 0.5,
    timeout: float = 10.0,
    verbose: bool = True,
) -> list[Organization]:
    """
    Отдельный шаг после основного сбора: для каждой организации с заполненным site
    пытается достать email с её сайта.
    """
    headers = {**DEFAULT_HEADERS, "accept": "text/html,application/xhtml+xml,*/*"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        for i, o in enumerate(orgs, 1):
            if not o.site:
                continue
            try:
                o.email = fetch_site_email(o.site, client=client)
            except Exception as exc:
                o.email = None
                if verbose:
                    print(f"[email] {o.site} ошибка: {exc!r}", file=sys.stderr)
            if verbose:
                print(f"[email] {i}/{len(orgs)} {o.site} -> {o.email or '—'}", file=sys.stderr)
            time.sleep(delay)
    return orgs


if __name__ == "__main__":
    import argparse
    import csv
    import sys

    ap = argparse.ArgumentParser(description="Парсер выдачи Яндекс.Карт")
    ap.add_argument("place",
                    help="город/регион кириллицей, напр. 'Санкт-Петербург' или "
                         "'Ленинградская область' — координаты определяются автоматически")
    ap.add_argument("query", help="что искать, напр. 'кофейни'")
    ap.add_argument("--url",
                     help="вместо place/query — готовый URL поиска Яндекс.Карт "
                          "(для ручной подстройки viewport; если задан, place/query игнорируются)")
    ap.add_argument("--headful", action="store_true", help="показать окно браузера")
    ap.add_argument("--no-grid", action="store_true",
                     help="не обходить весь регион сеткой, а сделать один поиск "
                          "по его общему viewport'у (быстрее, но с лимитом на кол-во организаций)")
    ap.add_argument("--bbox", metavar="MIN_LON,MIN_LAT,MAX_LON,MAX_LAT",
                     help="явный bbox для обхода сеткой вместо автоматически определённого "
                          "по place (снимает лимит организаций на один viewport)")
    ap.add_argument("--cell", metavar="LON,LAT", default="0.03,0.02",
                     help="стартовый размер ячейки сетки в градусах (по умолчанию 0.03,0.02)")
    ap.add_argument("--split-threshold", type=int, default=90,
                     help="ячейка с таким числом организаций и больше считается "
                          "обрезанной и дробится на 4 (по умолчанию 90)")
    ap.add_argument("--output", "-o", default="result.csv",
                     help="файл для CSV-результата (по умолчанию result.csv)")
    ap.add_argument("--emails", action="store_true",
                     help="отдельным шагом после сбора зайти на сайт каждой организации "
                          "и попытаться найти email (медленно, не всегда успешно)")
    ap.add_argument("--email-delay", type=float, default=0.5,
                     help="пауза между запросами к сайтам организаций, сек (по умолчанию 0.5)")
    ap.add_argument("--limit", type=int, default=None,
                     help="остановиться, как только собрано столько организаций "
                          "(по умолчанию — искать все)")
    args = ap.parse_args()

    bbox = None
    if args.url:
        search_url = args.url
    else:
        print(f"ищу координаты «{args.place}»...", file=sys.stderr)
        geo = geocode(args.place)
        search_url = build_search_url(args.query, center=geo["center"], span=geo["span"])
        if not args.no_grid:
            bbox = geo["bbox"]
        print(f"регион: центр {geo['center']}, bbox {geo['bbox']}", file=sys.stderr)

    if args.bbox:
        min_lon, min_lat, max_lon, max_lat = (float(x) for x in args.bbox.split(","))
        bbox = (min_lon, min_lat, max_lon, max_lat)

    if bbox:
        cell_lon, cell_lat = (float(x) for x in args.cell.split(","))
        orgs = search_grid(
            search_url,
            bbox,
            cell=(cell_lon, cell_lat),
            split_threshold=args.split_threshold,
            limit=args.limit,
            headless=not args.headful,
        )
    else:
        orgs = search_all_browser(search_url, headless=not args.headful, limit=args.limit)

    print(f"собрано организаций: {len(orgs)}", file=sys.stderr)
    if not orgs:
        sys.exit("ничего не собрано — проверь URL/селекторы скролла")

    if args.emails:
        enrich_emails(orgs, delay=args.email_delay)
        found = sum(1 for o in orgs if o.email)
        print(f"email найден у {found}/{len(orgs)}", file=sys.stderr)

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(orgs[0]).keys()))
        w.writeheader()
        for o in orgs:
            w.writerow(asdict(o))

    print(f"результат сохранён в {args.output}", file=sys.stderr)
