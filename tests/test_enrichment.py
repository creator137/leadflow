import pytest
from pydantic import ValidationError

from app.services.company_enrichment import AIEnrichmentResult, SYSTEM_PROMPT, WebsitePage, WebsiteSnapshot, _deterministic


def empty_result():
    return {
        "region": None, "branches_count": None, "decision_maker_name": None,
        "decision_maker_position": None, "decision_maker_email": None,
        "decision_maker_phone": None, "inn": None,
        "sources": {
            "region": None, "branches_count": None, "decision_maker_name": None,
            "decision_maker_position": None, "decision_maker_email": None,
            "decision_maker_phone": None, "inn": None,
        },
        "confidence": {
            "region": None, "branches_count": None, "decision_maker_name": None,
            "decision_maker_position": None, "decision_maker_email": None,
            "decision_maker_phone": None, "inn": None,
        },
    }


def test_ai_null_result_is_valid_and_prompt_forbids_hallucination() -> None:
    result = AIEnrichmentResult.model_validate(empty_result())
    assert result.decision_maker_name is None
    assert "Не используй догадки" in SYSTEM_PROMPT
    assert "Не генерируй email" in SYSTEM_PROMPT


def test_ai_value_requires_source_url_and_schema_is_strict() -> None:
    payload = empty_result()
    payload["decision_maker_name"] = "Анна Иванова"
    with pytest.raises(ValidationError):
        AIEnrichmentResult.model_validate(payload)

    valid = empty_result()
    valid["decision_maker_name"] = "Анна Иванова"
    valid["sources"]["decision_maker_name"] = "https://company.test/team"
    valid["confidence"]["decision_maker_name"] = 0.95
    assert AIEnrichmentResult.model_validate(valid).decision_maker_name == "Анна Иванова"
    payload["unexpected"] = "forbidden"
    with pytest.raises(ValidationError):
        AIEnrichmentResult.model_validate(payload)


def test_deterministic_structured_data_has_provenance() -> None:
    page = WebsitePage(
        url="https://company.test/contacts",
        text="Контакты компании",
        html='''<script type="application/ld+json">{"@type":"Organization","email":"info@company.test","telephone":"+7 495 111-22-33","taxID":"1234567890","address":{"addressRegion":"Москва"}}</script>''',
    )
    result = _deterministic(WebsiteSnapshot([page]))
    assert result["company_email"] == ("info@company.test", page.url, 0.99, "website_structured_data")
    assert result["inn"][0] == "1234567890"
