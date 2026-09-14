import json

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import AIRequestLog, AISettings, Company, EmailTemplate, WebsiteAnalysis
from app.services.company_enrichment import WebsiteAnalysisService, WebsitePage, WebsiteSnapshot
from app.services.deepseek import DeepSeekClient, DeepSeekError, _output_text
from app.services.personalized import PersonalizationFragments, _render


def company() -> Company:
    return Company(source="yandex_maps", company_name="Ромашка", normalized_name="ромашка",
                   website="https://company.test", raw_data={})


def response_body(content: dict, *, input_tokens: int = 120, output_tokens: int = 20) -> dict:
    return {"status": "completed", "model": "deepseek-flash", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(content, ensure_ascii=False)}
    ]}], "usage": {"input_tokens": input_tokens, "input_tokens_details": {"cached_tokens": 10},
          "output_tokens": output_tokens, "output_tokens_details": {"reasoning_tokens": 0}}}


def test_structured_output_accepts_only_an_outer_markdown_json_fence() -> None:
    payload = response_body({"value": "тест"})
    payload["output"][0]["content"][0]["text"] = '```json\n{"value":"тест"}\n```'
    assert json.loads(_output_text(payload)) == {"value": "тест"}


def test_responses_api_is_economic_strict_and_accounted() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=response_body({"subject": "Тема", "intro": "Вступление", "personalized_paragraph": "Факт"}))
    with Session(engine) as session:
        item = company(); session.add(item); session.flush()
        settings = Settings(_env_file=None, deepseek_api_key="test-secret")
        result = DeepSeekClient(session, settings, transport=httpx.MockTransport(handler)).generate(
            company_id=item.id, operation="email_personalization", content_hash="hash", missing_fields=["template"],
            prompt_version="v1", instructions="test", input_text="compact", response_model=PersonalizationFragments,
            max_output_tokens=250,
        )
        assert captured["model"] == "deepseek-flash"
        assert captured["reasoning"] == {"effort": "none"}
        assert captured["text"]["format"]["type"] == "json_schema"
        assert captured["max_output_tokens"] == 250 and captured["store"] is False
        assert result.usage.input_tokens == 120 and result.usage.cached_input_tokens == 10
        assert result.usage.output_tokens == 20 and result.usage.reasoning_tokens == 0
        assert session.scalar(select(AIRequestLog)).estimated_cost > 0


def test_identical_request_uses_ai_cache() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = 0
    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls; calls += 1
        return httpx.Response(200, json=response_body({"subject": "Тема", "intro": "Вступление", "personalized_paragraph": "Факт"}))
    with Session(engine) as session:
        item = company(); session.add(item); session.flush()
        client = DeepSeekClient(session, Settings(_env_file=None, deepseek_api_key="test-secret"), transport=httpx.MockTransport(handler))
        args = dict(company_id=item.id, operation="email_personalization", content_hash="hash", missing_fields=["t"],
                    prompt_version="v1", instructions="x", input_text="y", response_model=PersonalizationFragments)
        first = client.generate(**args); second = client.generate(**args)
        assert calls == 1 and not first.cache_hit and second.cache_hit
        assert session.query(AIRequestLog).count() == 2


def test_unauthorized_is_not_retried_and_secret_is_not_stored() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = 0
    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls; calls += 1
        return httpx.Response(401, json={"error": "bad key"})
    with Session(engine) as session:
        item = company(); session.add(item); session.flush()
        client = DeepSeekClient(session, Settings(_env_file=None, deepseek_api_key="very-secret"), transport=httpx.MockTransport(handler))
        try:
            client.generate(company_id=item.id, operation="email_personalization", content_hash="h", missing_fields=[],
                            prompt_version="v", instructions="x", input_text="y", response_model=PersonalizationFragments)
            assert False, "expected DeepSeekError"
        except DeepSeekError as exc:
            assert exc.code == "authentication"
        assert calls == 1
        log = session.scalar(select(AIRequestLog))
        assert "very-secret" not in json.dumps(log.response_data) and log.error_code == "authentication"


def test_daily_request_limit_blocks_network_call() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = 0
    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls; calls += 1
        return httpx.Response(500)
    with Session(engine) as session:
        item = company(); session.add_all([item, AISettings(id="default", daily_request_limit=1)]); session.flush()
        session.add(AIRequestLog(operation="website_enrichment", company_id=item.id, model="deepseek-flash",
            request_key="used", content_hash="h", missing_fields=[], prompt_version="v", success=True, cache_hit=False))
        session.flush()
        client = DeepSeekClient(session, Settings(_env_file=None, deepseek_api_key="test"), transport=httpx.MockTransport(handler))
        try:
            client.generate(company_id=item.id, operation="email_personalization", content_hash="h2", missing_fields=[],
                            prompt_version="v", instructions="x", input_text="y", response_model=PersonalizationFragments)
            assert False, "expected limit"
        except DeepSeekError as exc:
            assert exc.code == "daily_limit"
        assert calls == 0


def test_invalid_json_still_accounts_reported_usage() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    def handler(_request: httpx.Request) -> httpx.Response:
        body = response_body({"unexpected": True}, input_tokens=77, output_tokens=9)
        return httpx.Response(200, json=body)
    with Session(engine) as session:
        item = company(); session.add(item); session.flush()
        client = DeepSeekClient(session, Settings(_env_file=None, deepseek_api_key="test"), transport=httpx.MockTransport(handler))
        try:
            client.generate(company_id=item.id, operation="email_personalization", content_hash="h", missing_fields=[],
                            prompt_version="v", instructions="x", input_text="y", response_model=PersonalizationFragments)
            assert False, "expected invalid JSON"
        except DeepSeekError as exc:
            assert exc.code == "invalid_json"
        log = session.scalar(select(AIRequestLog))
        assert not log.success and log.input_tokens == 77 and log.output_tokens == 9


def test_deterministic_first_missing_only_and_failure_is_non_blocking(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    snapshot = WebsiteSnapshot([WebsitePage(
        "https://company.test/contacts", "Телефон +7 999 111-22-33. Генеральный директор Анна Иванова.", ""
    )])
    monkeypatch.setattr("app.services.company_enrichment.crawl_website", lambda *_args, **_kwargs: snapshot)
    class FailedClient:
        def generate(self, **kwargs):
            assert "company_phone" not in kwargs["missing_fields"]
            assert "decision_maker_name" not in kwargs["missing_fields"]
            raise DeepSeekError("ИИ временно недоступен.")
    with Session(engine) as session:
        item = company(); session.add(item); session.add(AISettings(id="default", enrichment_enabled=True)); session.flush()
        result = WebsiteAnalysisService(session, Settings(_env_file=None), deepseek=FailedClient()).enrich(item)
        assert item.company_phone and item.decision_maker_name == "Анна Иванова"
        assert result["deterministic"] == 2 and result["ai_error"] == "ИИ временно недоступен."
        assert session.get(Company, item.id) is not None


def test_website_analysis_cache_avoids_recrawl(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = 0
    def crawl(*_args, **_kwargs):
        nonlocal calls; calls += 1
        return WebsiteSnapshot([WebsitePage("https://company.test", "Публичный текст о компании и услугах. " * 3, "")])
    monkeypatch.setattr("app.services.company_enrichment.crawl_website", crawl)
    with Session(engine) as session:
        item = company(); session.add(item); session.flush()
        service = WebsiteAnalysisService(session, Settings(_env_file=None))
        first = service.enrich(item, use_ai=False); second = service.enrich(item, use_ai=False)
        assert calls == 1 and not first["website_cache_hit"] and second["website_cache_hit"]
        stored = session.scalar(select(WebsiteAnalysis).where(WebsiteAnalysis.company_id == item.id))
        assert stored.content_hash == first["content_hash"] == second["content_hash"]


def test_personalization_fragments_keep_commercial_template() -> None:
    item = company()
    template = EmailTemplate(name="Коммерческое предложение", subject_template="Предложение для {{ company_name }}",
        html_template="<p>Основная коммерческая часть.</p>", text_template="Основная коммерческая часть.")
    fragments = PersonalizationFragments(subject="Для Ромашки", intro="Здравствуйте!",
                                          personalized_paragraph="На вашем сайте указана услуга доставки.")
    rendered = _render(template, item, None, fragments)
    assert rendered["subject"] == "Для Ромашки"
    assert "услуга доставки" in rendered["html_body"]
    assert "Основная коммерческая часть" in rendered["html_body"]
