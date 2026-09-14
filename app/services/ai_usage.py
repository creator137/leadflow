from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import AIRequestLog, Company, WebsiteAnalysis


OPERATION_LABELS = {
    "website_enrichment": "Дополнение данных",
    "email_personalization": "Персональное письмо",
    "connection_test": "Проверка подключения",
    "phrase_search": "Поиск по фразам",
}
FIELD_LABELS = {
    "company_email": "Email компании",
    "company_phone": "Телефон компании",
    "website": "Сайт",
    "region": "Регион",
    "branches_count": "Количество филиалов",
    "decision_maker_name": "ЛПР",
    "decision_maker_position": "Должность ЛПР",
    "decision_maker_email": "Email ЛПР",
    "decision_maker_phone": "Телефон ЛПР",
    "inn": "ИНН",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _operation_label(operation: str) -> str:
    return OPERATION_LABELS.get(operation, "Другая операция")


def _status(log: AIRequestLog) -> tuple[str, str]:
    if log.cache_hit:
        return "cache_hit", "Из кэша"
    if log.success:
        return "success", "Успешно"
    return "error", "Ошибка"


def _result_summary(log: AIRequestLog) -> str:
    data = log.response_data or {}
    if not log.success:
        return "Запрос завершился с ошибкой"
    if log.operation == "connection_test":
        return "Подключение подтверждено"
    if log.operation == "email_personalization":
        subject = str(data.get("subject") or "").strip()
        return f"Подготовлена тема: {subject}" if subject else "Подготовлены фрагменты письма"
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    found = [FIELD_LABELS.get(name, name) for name, value in fields.items() if value not in (None, "")]
    return "Найдено: " + ", ".join(found) if found else "Новых достоверных данных не найдено"


def _serialize_row(log: AIRequestLog, company: Company | None) -> dict[str, Any]:
    status, status_label = _status(log)
    missing_fields = log.missing_fields or []
    return {
        "id": log.id,
        "created_at": _iso(log.created_at),
        "company_id": company.id if company else None,
        "company": company.company_name if company else "—",
        "website": company.website if company else None,
        "operation": log.operation,
        "operation_label": _operation_label(log.operation),
        "fields": [FIELD_LABELS.get(name, name) for name in missing_fields]
        if log.operation in {"website_enrichment", "phrase_search"}
        else [],
        "result": _result_summary(log),
        "input_tokens": log.input_tokens,
        "cached_input_tokens": log.cached_input_tokens,
        "output_tokens": log.output_tokens,
        "reasoning_tokens": log.reasoning_tokens,
        "model": log.model,
        "estimated_cost": round(log.estimated_cost or 0, 8),
        "status": status,
        "status_label": status_label,
    }


def ai_usage_journal(
    session: Session,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    company_id: str | None = None,
    operation: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    list_conditions = []
    common_conditions = []
    if company_id:
        common_conditions.append(AIRequestLog.company_id == company_id)
    if operation:
        common_conditions.append(AIRequestLog.operation == operation)
    list_conditions.extend(common_conditions)
    if date_from:
        list_conditions.append(AIRequestLog.created_at >= date_from)
    if date_to:
        list_conditions.append(AIRequestLog.created_at <= date_to)

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days = now - timedelta(days=7)
    thirty_days = now - timedelta(days=30)
    summary = session.execute(
        select(
            func.count(AIRequestLog.id),
            func.coalesce(func.sum(AIRequestLog.estimated_cost), 0.0),
            func.sum(case((AIRequestLog.created_at >= today, 1), else_=0)),
            func.coalesce(func.sum(case((AIRequestLog.created_at >= today, AIRequestLog.estimated_cost), else_=0.0)), 0.0),
            func.sum(case((AIRequestLog.created_at >= seven_days, 1), else_=0)),
            func.coalesce(func.sum(case((AIRequestLog.created_at >= seven_days, AIRequestLog.estimated_cost), else_=0.0)), 0.0),
            func.sum(case((AIRequestLog.created_at >= thirty_days, 1), else_=0)),
            func.coalesce(func.sum(case((AIRequestLog.created_at >= thirty_days, AIRequestLog.estimated_cost), else_=0.0)), 0.0),
        ).where(*common_conditions)
    ).one()

    breakdown_rows = session.execute(
        select(
            AIRequestLog.operation,
            func.count(AIRequestLog.id),
            func.coalesce(func.sum(AIRequestLog.estimated_cost), 0.0),
        )
        .where(*list_conditions)
        .group_by(AIRequestLog.operation)
        .order_by(AIRequestLog.operation)
    ).all()
    breakdown = {row.operation: {"requests": row[1], "cost": round(float(row[2]), 8)} for row in breakdown_rows}
    for key, label in OPERATION_LABELS.items():
        breakdown.setdefault(key, {"requests": 0, "cost": 0.0})
        breakdown[key]["label"] = label

    total = session.scalar(select(func.count(AIRequestLog.id)).where(*list_conditions)) or 0
    rows = session.execute(
        select(AIRequestLog, Company)
        .outerjoin(Company, Company.id == AIRequestLog.company_id)
        .where(*list_conditions)
        .order_by(AIRequestLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    companies = session.execute(
        select(Company.id, Company.company_name)
        .join(AIRequestLog, AIRequestLog.company_id == Company.id)
        .distinct()
        .order_by(Company.company_name)
    ).all()
    return {
        "summary": {
            "today": {"requests": summary[2] or 0, "cost": round(float(summary[3]), 8)},
            "seven_days": {"requests": summary[4] or 0, "cost": round(float(summary[5]), 8)},
            "thirty_days": {"requests": summary[6] or 0, "cost": round(float(summary[7]), 8)},
            "all": {"requests": summary[0] or 0, "cost": round(float(summary[1]), 8)},
        },
        "breakdown": [breakdown[key] | {"operation": key} for key in OPERATION_LABELS],
        "companies": [{"id": row.id, "name": row.company_name} for row in companies],
        "operations": [{"id": key, "name": value} for key, value in OPERATION_LABELS.items()],
        "rows": [_serialize_row(log, company) for log, company in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def ai_usage_details(session: Session, log_id: str) -> dict[str, Any] | None:
    record = session.execute(
        select(AIRequestLog, Company, WebsiteAnalysis)
        .outerjoin(Company, Company.id == AIRequestLog.company_id)
        .outerjoin(WebsiteAnalysis, WebsiteAnalysis.company_id == AIRequestLog.company_id)
        .where(AIRequestLog.id == log_id)
    ).one_or_none()
    if not record:
        return None
    log, company, analysis = record
    row = _serialize_row(log, company)
    exact_context = bool(analysis and log.content_hash and analysis.content_hash == log.content_hash)
    data = log.response_data or {}
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    found_values = []
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    for name, value in fields.items():
        if value in (None, ""):
            continue
        proof = next(
            (
                item
                for item in evidence
                if isinstance(item, dict) and item.get("field") == name and str(item.get("value")) == str(value)
            ),
            {},
        )
        found_values.append(
            {
                "field": name,
                "label": FIELD_LABELS.get(name, name),
                "value": value,
                "source_url": proof.get("source_url"),
                "evidence_text": proof.get("evidence_text"),
            }
        )
    if log.operation == "website_enrichment":
        reason = "На сайте компании искались только пустые поля: " + (
            ", ".join(row["fields"]) if row["fields"] else "список полей не сохранён"
        )
        context = (analysis.page_blocks or []) if exact_context else []
    elif log.operation == "email_personalization":
        reason = "Пользователь запросил персональное письмо на основании проверенных фактов о компании."
        context = (analysis.facts or []) if exact_context else []
    else:
        reason = "Пользователь проверил подключение LeadFlow к DeepSeek."
        context = []
    return row | {
        "reason": reason,
        "context_available": exact_context,
        "website_context": [
            {
                "source_url": item.get("url") or item.get("source_url"),
                "text": str(item.get("text") or "")[:1500],
            }
            for item in context[:6]
            if isinstance(item, dict) and item.get("text")
        ],
        "found_values": found_values,
        "evidence": evidence,
        "personalization": {
            "subject": data.get("subject"),
            "intro": data.get("intro"),
            "personalized_paragraph": data.get("personalized_paragraph"),
        }
        if log.operation == "email_personalization"
        else None,
        "error": "Запрос завершился с ошибкой." if not log.success else None,
    }
