from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_help_is_available_from_main_navigation() -> None:
    app_script = (ROOT / "app/static/app.js").read_text()
    html = (ROOT / "app/static/index.html").read_text()

    assert "help:['Помощь / Инструкция','Пошаговая работа с LeadFlow']" in app_script
    assert "id=\"section-help\"" in html
    assert "/static/help.js" in html
    assert "/static/help.css" in html


def test_help_covers_every_visible_admin_section_and_critical_workflows() -> None:
    help_text = (ROOT / "app/static/help.js").read_text()
    visible_sections = (
        "Главная", "Компании", "Направления", "Поиск по фразам", "Поиск товаров",
        "Письма и КП", "Шаблоны", "Шаблоны КП", "Автоматические рассылки",
        "Данные отправителя", "Почтовые ящики", "Google Таблица", "Аналитика", "Настройки",
    )
    for section in visible_sections:
        assert section in help_text

    for required_instruction in (
        "Подготовить КП", "Отправить тест", "Отправить письмо", "Синхронизировать сейчас",
        "Куда пересылать ответы клиентов", "Дневной лимит", "Вложения", "Типовые ошибки",
    ):
        assert required_instruction in help_text


def test_help_uses_real_ui_labels_for_direction_and_proposal_fields() -> None:
    help_text = (ROOT / "app/static/help.js").read_text()
    html = (ROOT / "app/static/index.html").read_text()
    labels = (
        "Название направления", "Что искать", "Где искать", "Новых компаний за запуск",
        "Приветствие", "Дополнительный блок", "Основное предложение",
        "Персонализация ИИ", "Призыв к действию", "Email для теста",
    )
    for label in labels:
        assert label in help_text
        assert label in html
