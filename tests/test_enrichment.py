from app.services.company_enrichment import (
    AIEnrichmentResult, AI_INSTRUCTIONS, Evidence, WebsitePage, WebsiteSnapshot,
    _deterministic, _validated_ai_values,
)


def test_ai_null_result_is_valid_and_prompt_forbids_hallucination() -> None:
    result = AIEnrichmentResult(fields={"decision_maker_name": None}, evidence=[])
    assert result.fields["decision_maker_name"] is None
    assert "не делай предположений" in AI_INSTRUCTIONS
    assert "Не создавай email" in AI_INSTRUCTIONS


def test_ai_value_requires_exact_evidence_and_known_url() -> None:
    page = WebsitePage("https://company.test/team", "Генеральный директор Анна Иванова", "")
    snapshot = WebsiteSnapshot([page])
    valid = AIEnrichmentResult(fields={"decision_maker_name": "Анна Иванова"}, evidence=[
        Evidence(field="decision_maker_name", value="Анна Иванова", source_url=page.url,
                 evidence_text="Генеральный директор Анна Иванова")
    ])
    assert _validated_ai_values(valid, snapshot, ["decision_maker_name"])["decision_maker_name"][0] == "Анна Иванова"
    invented = AIEnrichmentResult(fields={"decision_maker_email": "anna@company.test"}, evidence=[
        Evidence(field="decision_maker_email", value="anna@company.test", source_url=page.url,
                 evidence_text="Генеральный директор Анна Иванова")
    ])
    assert _validated_ai_values(invented, snapshot, ["decision_maker_email"]) == {}


def test_ai_does_not_accept_invented_branch_count() -> None:
    page = WebsitePage("https://company.test/about", "Мы работаем по всей России.", "")
    result = AIEnrichmentResult(fields={"branches_count": 12}, evidence=[
        Evidence(field="branches_count", value=12, source_url=page.url, evidence_text=page.text)
    ])
    assert _validated_ai_values(result, WebsiteSnapshot([page]), ["branches_count"]) == {}


def test_ai_accepts_only_explicit_management_role_for_decision_maker() -> None:
    chef_page = WebsitePage("https://company.test/team", "Концепт-шеф Александр Ермаков", "")
    chef = AIEnrichmentResult(fields={"decision_maker_name": "Александр Ермаков", "decision_maker_position": "Концепт-шеф"}, evidence=[
        Evidence(field="decision_maker_name", value="Александр Ермаков", source_url=chef_page.url, evidence_text=chef_page.text),
        Evidence(field="decision_maker_position", value="Концепт-шеф", source_url=chef_page.url, evidence_text=chef_page.text),
    ])
    assert _validated_ai_values(chef, WebsiteSnapshot([chef_page]), list(chef.fields)) == {}
    director_page = WebsitePage("https://company.test/team", "Генеральный директор Анна Иванова", "")
    director = AIEnrichmentResult(fields={"decision_maker_name": "Анна Иванова", "decision_maker_position": "Генеральный директор"}, evidence=[
        Evidence(field="decision_maker_name", value="Анна Иванова", source_url=director_page.url, evidence_text=director_page.text),
        Evidence(field="decision_maker_position", value="Генеральный директор", source_url=director_page.url, evidence_text=director_page.text),
    ])
    assert set(_validated_ai_values(director, WebsiteSnapshot([director_page]), list(director.fields))) == set(director.fields)


def test_deterministic_structured_data_has_provenance() -> None:
    page = WebsitePage(
        url="https://company.test/contacts", text="Контакты компании",
        html='''<script type="application/ld+json">{"@type":"Organization","email":"info@company.test","telephone":"+7 495 111-22-33","taxID":"1234567890","address":{"addressRegion":"Москва"}}</script>''',
    )
    result = _deterministic(WebsiteSnapshot([page]))
    assert result["company_email"] == ("info@company.test", page.url, 0.99, "website_structured_data")
    assert result["inn"][0] == "1234567890"


def test_compact_context_keeps_each_relevant_page_within_limit() -> None:
    snapshot = WebsiteSnapshot([
        WebsitePage("https://company.test", "A" * 5000),
        WebsitePage("https://company.test/contacts", "B" * 5000),
        WebsitePage("https://company.test/team", "C" * 5000),
    ])
    blocks = snapshot.compact_blocks(3000)
    assert [block["url"] for block in blocks] == [page.url for page in snapshot.pages]
    assert sum(len(block["text"]) for block in blocks) == 3000
