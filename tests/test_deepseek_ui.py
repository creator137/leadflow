from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_ai_settings_and_company_actions_are_plain_russian_ui() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    for label in (
        "Дополнять пустые данные компаний", "Использовать ИИ для персональных писем",
        "Лимит запросов в сутки", "Проверить подключение", "Подготовить персональное письмо",
        "Дополнить данные", "Новых достоверных данных не найдено",
    ):
        assert label in html + javascript
    assert "DEEPSEEK_API_KEY" not in html + javascript


def test_personalized_send_requires_preview_confirmation_in_ui() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert 'id="sendRequestKey"' in html
    assert "personalization-preview" in javascript
    assert "send-personalized" in javascript
    assert "Проверьте его перед отправкой" in javascript
    assert "Подготовить персональное КП" in html
    assert 'id="sendDraftId"' in html
    assert "personalization-drafts" in javascript
    assert "Подтвердить отправку" in html
