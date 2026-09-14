from app.services.google_sheets import BUSINESS_COLUMNS
from app.sources.phrase_search.service import FreeSearchProvider, _page_evidence


def test_free_discovery_accepts_only_real_public_urls() -> None:
    assert FreeSearchProvider._clean_url("https://example.org/company") == "https://example.org/company"
    assert FreeSearchProvider._clean_url("javascript:alert(1)") is None
    assert FreeSearchProvider._clean_url("https://www.bing.com/search?q=x") is None
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fcontacts"
    assert FreeSearchProvider._clean_url(wrapped) == "https://example.org/contacts"


def test_phrase_result_requires_page_evidence() -> None:
    text = "Компания Ромашка оказывает услуги кейтеринга для мероприятий в Екатеринбурге."
    assert _page_evidence(text, "кейтеринг для мероприятий") in text
    assert _page_evidence(text, "продажа автомобилей") is None


def test_site_column_is_immediately_after_company_phone() -> None:
    fields = [field for _title, field in BUSINESS_COLUMNS]
    assert fields.index("website") == fields.index("company_phone") + 1


def test_business_ui_has_phrase_search_and_sheet_actions() -> None:
    html = open("app/static/index.html", encoding="utf-8").read()
    js = open("app/static/app.js", encoding="utf-8").read()
    apps_script = open("integrations/google_sheets/LeadFlow.gs", encoding="utf-8").read()
    assert "Поиск по фразам" in js and "section-phraseSearch" in html
    assert "Подготовить КП" in apps_script and "Отправить КП" in apps_script
    assert "X-LeadFlow-Sheet-Token" in apps_script and "LeadFlow ID" in apps_script
    assert "PropertiesService.getScriptProperties" in apps_script
