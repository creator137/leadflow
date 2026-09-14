from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_ai_settings_and_company_actions_are_plain_russian_ui() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    for label in (
        "Дополнять пустые данные компаний", "Использовать ИИ для персональных писем",
        "Лимит запросов в сутки", "Проверить подключение", "Подготовить КП",
        "Дополнить данные", "Новых достоверных данных не найдено",
    ):
        assert label in html + javascript
    assert "DEEPSEEK_API_KEY" not in html + javascript


def test_proposal_draft_requires_separate_preview_and_send_in_ui() -> None:
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert 'id="proposalDraftId"' in html
    assert "/proposal-drafts/" in javascript
    assert "confirm-send" in javascript
    assert "Черновик КП подготовлен. Письмо не отправлено." in javascript
    assert "Подготовить персональное КП" in html
    assert "Сохранить черновик" in html
    assert "Предпросмотр" in html
    assert "Отправить письмо" in html
