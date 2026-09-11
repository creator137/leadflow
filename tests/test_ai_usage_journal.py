from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import AIRequestLog, Company, WebsiteAnalysis
from app.services.ai_usage import ai_usage_details, ai_usage_journal


def test_ai_usage_journal_uses_existing_accounting_and_filters() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        company = Company(
            source="yandex_maps",
            company_name="Ромашка",
            normalized_name="ромашка",
            website="https://example.ru",
            raw_data={},
        )
        session.add(company)
        session.flush()
        session.add_all(
            [
                AIRequestLog(
                    operation="website_enrichment",
                    company_id=company.id,
                    model="deepseek-flash",
                    request_key="enrichment",
                    content_hash="content",
                    missing_fields=["company_email", "branches_count"],
                    prompt_version="v1",
                    response_data={
                        "fields": {"company_email": "info@example.ru", "branches_count": None},
                        "evidence": [
                            {
                                "field": "company_email",
                                "value": "info@example.ru",
                                "source_url": "https://example.ru/contacts",
                                "evidence_text": "Email: info@example.ru",
                            }
                        ],
                    },
                    input_tokens=120,
                    cached_input_tokens=10,
                    output_tokens=20,
                    reasoning_tokens=0,
                    estimated_cost=0.00006,
                    success=True,
                    created_at=now,
                ),
                AIRequestLog(
                    operation="email_personalization",
                    company_id=company.id,
                    model="deepseek-flash",
                    request_key="personalization",
                    content_hash="content",
                    missing_fields=["template-id"],
                    prompt_version="v1",
                    response_data={"subject": "Для Ромашки", "intro": "Здравствуйте", "personalized_paragraph": "Факт"},
                    input_tokens=80,
                    output_tokens=15,
                    estimated_cost=0.00004,
                    success=True,
                    cache_hit=True,
                    created_at=now - timedelta(days=2),
                ),
            ]
        )
        session.commit()

        result = ai_usage_journal(session, company_id=company.id)
        assert result["summary"]["all"] == {"requests": 2, "cost": 0.0001}
        assert result["summary"]["today"] == {"requests": 1, "cost": 0.00006}
        assert len(result["rows"]) == 2
        assert result["rows"][0]["fields"] == ["Email компании", "Количество филиалов"]
        assert result["rows"][1]["status_label"] == "Из кэша"

        filtered = ai_usage_journal(session, operation="website_enrichment", date_from=now - timedelta(hours=1))
        assert filtered["total"] == 1
        assert filtered["rows"][0]["result"] == "Найдено: Email компании"


def test_ai_usage_details_returns_public_evidence_and_exact_website_context() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        company = Company(
            source="two_gis",
            company_name="Лилия",
            normalized_name="лилия",
            website="https://example.ru",
            raw_data={},
        )
        session.add(company)
        session.flush()
        analysis = WebsiteAnalysis(
            company_id=company.id,
            website=company.website,
            content_hash="same-content",
            page_blocks=[{"url": "https://example.ru/contacts", "text": "Email: info@example.ru"}],
            facts=[{"source_url": "https://example.ru", "text": "Компания оказывает услуги."}],
        )
        log = AIRequestLog(
            operation="website_enrichment",
            company_id=company.id,
            model="deepseek-flash",
            request_key="detail",
            content_hash="same-content",
            missing_fields=["company_email"],
            prompt_version="v1",
            response_data={
                "fields": {"company_email": "info@example.ru"},
                "evidence": [
                    {
                        "field": "company_email",
                        "value": "info@example.ru",
                        "source_url": "https://example.ru/contacts",
                        "evidence_text": "Email: info@example.ru",
                    }
                ],
            },
            success=True,
        )
        session.add_all([analysis, log])
        session.commit()

        result = ai_usage_details(session, log.id)
        assert result is not None
        assert result["reason"].startswith("На сайте компании искались только пустые поля")
        assert result["context_available"] is True
        assert result["website_context"][0]["text"] == "Email: info@example.ru"
        assert result["found_values"][0]["source_url"] == "https://example.ru/contacts"
        assert "request_key" not in result
