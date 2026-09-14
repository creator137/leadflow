"""Compare exact PostgreSQL, analytics API, and rendered UI values for today."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import httpx
from playwright.sync_api import sync_playwright
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Company, EmailDelivery, SearchObservation


def run() -> None:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as session:
        sql = {
            "new": session.scalar(select(func.count()).select_from(SearchObservation).where(SearchObservation.observed_at >= start, SearchObservation.company_id.is_not(None), SearchObservation.is_new.is_(True))) or 0,
            "duplicates": session.scalar(select(func.count()).select_from(SearchObservation).where(SearchObservation.observed_at >= start, SearchObservation.company_id.is_not(None), SearchObservation.is_new.is_(False))) or 0,
            "yandex": session.scalar(select(func.count()).select_from(SearchObservation).where(SearchObservation.observed_at >= start, SearchObservation.company_id.is_not(None), SearchObservation.source == "yandex_maps")) or 0,
            "two_gis": session.scalar(select(func.count()).select_from(SearchObservation).where(SearchObservation.observed_at >= start, SearchObservation.company_id.is_not(None), SearchObservation.source == "two_gis")) or 0,
            "with_website": session.scalar(select(func.count()).select_from(Company).where(Company.created_at >= start, Company.website.is_not(None))) or 0,
            "with_email": session.scalar(select(func.count()).select_from(Company).where(Company.created_at >= start, func.coalesce(Company.company_email, Company.email).is_not(None))) or 0,
            "with_phone": session.scalar(select(func.count()).select_from(Company).where(Company.created_at >= start, func.coalesce(Company.company_phone, Company.phone).is_not(None))) or 0,
            "sent": session.scalar(select(func.count()).select_from(EmailDelivery).where(EmailDelivery.created_at >= start, EmailDelivery.sent_at.is_not(None))) or 0,
            "replied": session.scalar(select(func.count()).select_from(EmailDelivery).where(EmailDelivery.created_at >= start, EmailDelivery.replied_at.is_not(None))) or 0,
            "bounced": session.scalar(select(func.count()).select_from(EmailDelivery).where(EmailDelivery.created_at >= start, EmailDelivery.bounced_at.is_not(None))) or 0,
            "unsubscribed": session.scalar(select(func.count()).select_from(EmailDelivery).where(EmailDelivery.created_at >= start, EmailDelivery.unsubscribed_at.is_not(None))) or 0,
        }
    auth = (os.getenv("ADMIN_USERNAME", ""), os.getenv("ADMIN_PASSWORD", ""))
    api_data = httpx.get("http://127.0.0.1:8000/api/analytics", params={"date_from": start.isoformat()}, auth=auth, timeout=30).json()
    sources = {row["name"]: row["observations"] for row in api_data["sources"]["rows"]}
    api = {"new": api_data["overview"]["new"], "duplicates": api_data["overview"]["duplicates"],
           "yandex": sources.get("yandex_maps", 0), "two_gis": sources.get("two_gis", 0),
           "with_website": api_data["overview"]["with_website"], "with_email": api_data["overview"]["with_email"],
           "with_phone": api_data["overview"]["with_phone"], "sent": api_data["email"]["sent"],
           "replied": api_data["email"]["replied"], "bounced": api_data["email"]["bounced"],
           "unsubscribed": api_data["email"]["unsubscribed"]}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(http_credentials={"username": auth[0], "password": auth[1]}, locale="ru-RU")
        page = context.new_page()
        bootstrap_stub = """window.bootstrap={
          Modal:class{show(){} hide(){}},
          Toast:{getOrCreateInstance:()=>({show(){}})},
          Offcanvas:{getOrCreateInstance:()=>({show(){}})}
        };"""
        page.route(
            "https://cdn.jsdelivr.net/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/css" if route.request.resource_type == "stylesheet" else "application/javascript",
                body="" if route.request.resource_type == "stylesheet" else bootstrap_stub,
            ),
        )
        page.goto("http://127.0.0.1:8000/#analytics", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_selector("#analyticsBody .metric-card", timeout=60_000)
        integer_labels = {
            "Новых уникальных", "Дубликатов", "Компаний с сайтом", "Компаний с email",
            "Компаний с телефоном", "Отправлено писем", "Ответов", "Не доставлено", "Отписок",
        }
        ui_cards = {
            card.locator(".metric-label").inner_text(): int(card.locator(".metric-value").inner_text())
            for card in page.locator("#analyticsBody .metric-card").all()
            if card.locator(".metric-label").inner_text() in integer_labels
        }
        ui = {"new": ui_cards["Новых уникальных"], "duplicates": ui_cards["Дубликатов"],
              "with_website": ui_cards["Компаний с сайтом"], "with_email": ui_cards["Компаний с email"],
              "with_phone": ui_cards["Компаний с телефоном"], "sent": ui_cards["Отправлено писем"],
              "replied": ui_cards["Ответов"], "bounced": ui_cards["Не доставлено"],
              "unsubscribed": ui_cards["Отписок"]}
        page.locator("#analyticsTabs [data-tab='sources']").click()
        source_rows = page.locator("#analyticsBody tbody tr").all()
        source_ui = {row.locator("td").nth(0).inner_text().strip(): int(row.locator("td").nth(1).inner_text()) for row in source_rows}
        ui["yandex"] = source_ui.get("Яндекс Карты", 0)
        ui["two_gis"] = source_ui.get("2ГИС", 0)
        browser.close()
    compared = {key: {"sql": sql[key], "api": api[key], "ui": ui.get(key), "pass": sql[key] == api[key] and (key not in ui or api[key] == ui[key])} for key in sql}
    print(json.dumps(compared, ensure_ascii=False))


if __name__ == "__main__":
    run()
