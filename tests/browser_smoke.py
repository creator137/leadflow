"""Real-browser smoke for the Russian admin UI. Run explicitly inside the api image."""
from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


PAGES = ["home", "companies", "directions", "letters", "templates", "mailboxes", "sheets", "analytics", "settings"]
VIEWPORTS = [(1920, 1080, "desktop"), (1366, 768, "laptop"), (390, 844, "mobile")]


def run() -> None:
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for width, height, label in VIEWPORTS:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                http_credentials={"username": os.getenv("ADMIN_USERNAME", ""), "password": os.getenv("ADMIN_PASSWORD", "")},
                locale="ru-RU",
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            response = page.goto("http://127.0.0.1:8000/", wait_until="networkidle", timeout=60_000)
            page.wait_for_selector("#homeMetrics .metric-card", timeout=30_000)
            for section in PAGES:
                if width < 992:
                    page.locator(".navbar-toggler").click()
                    page.locator(f"#mobileMenu [data-section='{section}']").click()
                else:
                    page.locator(f"#desktopMenu [data-section='{section}']").click()
                page.wait_for_selector(f"#section-{section}:not(.d-none)")
                page.wait_for_timeout(500)
            page.locator("#desktopMenu [data-section='analytics']" if width >= 992 else ".navbar-toggler").click()
            if width < 992:
                page.locator("#mobileMenu [data-section='analytics']").click()
            page.wait_for_selector("#analyticsBody .metric-card", timeout=30_000)
            for tab in ["search", "companies", "email", "replies", "sources", "sheets", "system"]:
                page.locator(f"#analyticsTabs [data-tab='{tab}']").click()
                page.wait_for_timeout(150)
                assert page.locator("#analyticsBody").inner_text().strip()
            page.evaluate("window.scrollTo(0, 0)")
            overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
            screenshot = f"/tmp/leadflow-{label}.png"
            page.screenshot(path=screenshot, full_page=True)
            results.append({"viewport": label, "status": response.status if response else 0, "overflow": overflow, "console_errors": errors, "screenshot": screenshot})
            context.close()
        browser.close()
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    run()
