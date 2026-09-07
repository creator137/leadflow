from app.models import Company, EmailTemplate
from app.services.mailing import render_template


def test_template_variables() -> None:
    company = Company(
        source="yandex_maps", company_name="Ромашка", category="кафе", city="Москва",
        normalized_name="ромашка", raw_data={},
    )
    template = EmailTemplate(
        name="test",
        subject_template="Предложение для {{ company_name }}",
        html_template="<p>{{ city }} — {{ category }}: {{ personalized_text }}</p>",
    )
    subject, html = render_template(template, company, {"personalized_text": "Текст"})
    assert subject == "Предложение для Ромашка"
    assert "Москва — кафе: Текст" in html
