from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from app.models import (
    AIRequestLog, Company, CompanyDirection, CompanyFieldProvenance, Direction, DirectionRun,
    EmailDelivery, EmailEvent, EmailTemplate, GoogleSheetsConfig, GoogleSyncRun, InboundReply,
    MailAccount, SearchObservation, SheetRowMapping, Suppression,
)
from app.services.user_errors import human_error
from app.config import get_settings


def _n(value: Any) -> int:
    return int(value or 0)


def _rate(part: int, total: int) -> float:
    return round(part * 100 / total, 1) if total else 0.0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def analytics(
    session: Session, *, date_from: datetime | None = None, date_to: datetime | None = None,
    direction: str | None = None, city: str | None = None, source: str | None = None,
    mailbox: str | None = None, template: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = date_from or (now - timedelta(days=30))
    end = date_to or now

    company_conditions = [Company.created_at >= start, Company.created_at <= end]
    if city: company_conditions.append(Company.city == city)
    if source: company_conditions.append(Company.source == source)
    company_stmt = select(Company)
    if direction:
        company_stmt = company_stmt.join(CompanyDirection).where(CompanyDirection.direction_id == direction)
    companies = list(session.scalars(company_stmt.where(*company_conditions)))
    company_ids = {company.id for company in companies}

    observation_conditions = [SearchObservation.observed_at >= start, SearchObservation.observed_at <= end]
    if direction: observation_conditions.append(SearchObservation.direction_id == direction)
    if city: observation_conditions.append(SearchObservation.city == city)
    if source: observation_conditions.append(SearchObservation.source == source)
    observations = list(session.scalars(select(SearchObservation).where(*observation_conditions)))
    result_observations = [item for item in observations if item.company_id]
    search_errors = [item for item in observations if item.error_type]

    delivery_conditions = [EmailDelivery.created_at >= start, EmailDelivery.created_at <= end]
    if direction: delivery_conditions.append(EmailDelivery.direction_id == direction)
    if mailbox: delivery_conditions.append(EmailDelivery.mailbox_id == mailbox)
    if template: delivery_conditions.append(EmailDelivery.template_id == template)
    if city or source:
        delivery_stmt = select(EmailDelivery).join(Company).where(*delivery_conditions)
        if city: delivery_stmt = delivery_stmt.where(Company.city == city)
        if source: delivery_stmt = delivery_stmt.where(Company.source == source)
    else:
        delivery_stmt = select(EmailDelivery).where(*delivery_conditions)
    deliveries = list(session.scalars(delivery_stmt))
    delivery_ids = {item.id for item in deliveries}
    historical_send_errors = _n(session.scalar(select(func.count(distinct(EmailEvent.delivery_id))).where(
        EmailEvent.delivery_id.in_(delivery_ids), EmailEvent.event_type == "send_error",
        EmailEvent.occurred_at >= start, EmailEvent.occurred_at <= end,
    ))) if delivery_ids else 0

    sent = sum(item.sent_at is not None for item in deliveries)
    email_counts = {
        "queued": sum(item.status in {"queued", "sending"} for item in deliveries),
        "sent": sent,
        "errors": max(historical_send_errors, sum(item.status in {"failed", "send_error"} for item in deliveries)),
        "opened": sum(item.opened_at is not None for item in deliveries),
        "clicked": sum(item.clicked_at is not None for item in deliveries),
        "replied": sum(item.replied_at is not None for item in deliveries),
        "bounced": sum(item.bounced_at is not None for item in deliveries),
        "unsubscribed": sum(item.unsubscribed_at is not None for item in deliveries),
    }
    email_counts["delivered"] = max(0, sent - email_counts["bounced"])
    email_counts["rates"] = {
        "delivery": _rate(email_counts["delivered"], sent),
        "open": _rate(email_counts["opened"], sent),
        "click": _rate(email_counts["clicked"], sent),
        "reply": _rate(email_counts["replied"], sent),
        "bounce": _rate(email_counts["bounced"], sent),
        "unsubscribe": _rate(email_counts["unsubscribed"], sent),
    }

    quality_fields = {
        "address": "Адрес", "company_phone": "Телефон", "company_email": "Email",
        "website": "Сайт", "branches_count": "Кол-во филиалов",
        "decision_maker_name": "ЛПР", "decision_maker_email": "Email ЛПР",
        "decision_maker_phone": "Телефон ЛПР",
    }
    quality = []
    for field, label in quality_fields.items():
        count = sum(getattr(company, field) not in (None, "") for company in companies)
        quality.append({"field": field, "label": label, "count": count, "percent": _rate(count, len(companies))})

    def grouped_observations(key: str) -> list[dict[str, Any]]:
        groups: dict[str, list[SearchObservation]] = defaultdict(list)
        for item in result_observations:
            groups[str(getattr(item, key) or "Не указано")].append(item)
        result = []
        for name, items in groups.items():
            total = len(items); new = sum(item.is_new for item in items)
            result.append({
                "name": name, "observations": total, "unique_companies": len({x.company_id for x in items}),
                "new_companies": new, "duplicates": total - new, "unique_rate": _rate(new, total),
                "with_phone": sum(x.has_phone for x in items), "with_email": sum(x.has_email for x in items),
                "with_website": sum(x.has_website for x in items),
                "with_branches_count": sum(x.has_branches_count for x in items),
                "errors": sum(x.error_type is not None for x in observations if str(getattr(x, key) or "Не указано") == name),
                "average_response_ms": round(sum(x.duration_ms or 0 for x in items) / total) if total else 0,
            })
        return sorted(result, key=lambda row: (-row["new_companies"], row["name"]))

    source_rows = grouped_observations("source")
    source_names = {"yandex_maps": "Яндекс Карты", "two_gis": "2ГИС"}
    for row in source_rows: row["label"] = source_names.get(row["name"], row["name"])
    insights = []
    if len(source_rows) >= 2:
        by_phones = max(source_rows, key=lambda row: _rate(row["with_phone"], row["observations"]))
        by_new = max(source_rows, key=lambda row: row["new_companies"])
        insights = [
            f"{source_names.get(by_phones['name'], by_phones['name'])} чаще находит телефоны.",
            f"{source_names.get(by_new['name'], by_new['name'])} дал больше новых компаний за выбранный период.",
        ]

    direction_names = dict(session.execute(select(Direction.id, Direction.name)).all())
    direction_groups: dict[str, dict[str, Any]] = {}
    for item in observations:
        row = direction_groups.setdefault(item.direction_id, {"id": item.direction_id, "name": direction_names.get(item.direction_id, "Направление"), "observations": 0, "new": 0, "duplicates": 0, "errors": 0, "last_run": None})
        if item.company_id:
            row["observations"] += 1; row["new"] += int(item.is_new); row["duplicates"] += int(not item.is_new)
        row["errors"] += int(bool(item.error_type))
        row["last_run"] = max(filter(None, [row["last_run"], item.observed_at]), default=None)
    for row in direction_groups.values(): row["last_run"] = _iso(row["last_run"])

    day_rows: dict[str, dict[str, int]] = defaultdict(lambda: {"found": 0, "new": 0, "sent": 0, "replied": 0})
    for item in result_observations:
        day = item.observed_at.date().isoformat(); day_rows[day]["found"] += 1; day_rows[day]["new"] += int(item.is_new)
    for item in deliveries:
        day = item.created_at.date().isoformat(); day_rows[day]["sent"] += int(item.sent_at is not None); day_rows[day]["replied"] += int(item.replied_at is not None)
    daily = [{"date": key, **value} for key, value in sorted(day_rows.items())]

    branch_known = [company for company in companies if company.branches_count is not None]
    provenance = list(session.scalars(select(CompanyFieldProvenance).where(
        CompanyFieldProvenance.company_id.in_(company_ids or {""}),
        CompanyFieldProvenance.field.in_(["branches_count", "website"]),
    )))
    branch_methods = defaultdict(int); website_methods = defaultdict(int)
    for item in provenance:
        target = branch_methods if item.field == "branches_count" else website_methods
        target[item.discovery_method] += 1

    mailbox_names = dict(session.execute(select(MailAccount.id, MailAccount.name)).all())
    template_names = dict(session.execute(select(EmailTemplate.id, EmailTemplate.name)).all())
    mailbox_rows = []
    for account in session.scalars(select(MailAccount)):
        items = [item for item in deliveries if item.mailbox_id == account.id]
        mailbox_rows.append({
            "id": account.id, "name": account.name, "email": account.from_email, "active": account.active,
            "daily_limit": account.daily_limit, "sent": sum(x.sent_at is not None for x in items),
            "errors": sum(x.status in {"failed", "send_error"} for x in items),
            "replies": sum(x.replied_at is not None for x in items), "bounces": sum(x.bounced_at is not None for x in items),
            "opens": sum(x.opened_at is not None for x in items), "clicks": sum(x.clicked_at is not None for x in items),
            "unsubscribes": sum(x.unsubscribed_at is not None for x in items),
            "last_sent": _iso(max((x.sent_at for x in items if x.sent_at), default=None)),
            "last_inbound_check": _iso(account.updated_at),
        })
    template_rows = []
    for item_id, name in template_names.items():
        items = [item for item in deliveries if item.template_id == item_id]
        template_rows.append({"id": item_id, "name": name, "sent": sum(x.sent_at is not None for x in items),
            "opened": sum(x.opened_at is not None for x in items), "clicked": sum(x.clicked_at is not None for x in items),
            "replied": sum(x.replied_at is not None for x in items), "bounced": sum(x.bounced_at is not None for x in items),
            "unsubscribed": sum(x.unsubscribed_at is not None for x in items)})

    reply_rows = []
    reply_stmt = select(InboundReply, EmailDelivery, Company).outerjoin(EmailDelivery, InboundReply.delivery_id == EmailDelivery.id).outerjoin(Company, EmailDelivery.company_id == Company.id).where(InboundReply.received_at >= start, InboundReply.received_at <= end).order_by(InboundReply.received_at.desc()).limit(50)
    for reply, delivery, company in session.execute(reply_stmt):
        reply_rows.append({"company": company.company_name if company else reply.sender, "date": _iso(reply.received_at),
            "direction": direction_names.get(delivery.direction_id) if delivery else None,
            "subject": reply.subject or "Без темы", "mailbox": mailbox_names.get(reply.mailbox_id, "Почтовый ящик")})

    sync_conditions = [GoogleSyncRun.started_at >= start, GoogleSyncRun.started_at <= end]
    if direction: sync_conditions.append(GoogleSyncRun.direction_id == direction)
    sync_runs = list(session.scalars(select(GoogleSyncRun).where(*sync_conditions).order_by(GoogleSyncRun.started_at.desc())))
    last_sync = sync_runs[0] if sync_runs else session.scalar(select(GoogleSyncRun).order_by(GoogleSyncRun.started_at.desc()).limit(1))
    sheets = {
        "configured": bool(session.scalar(select(func.count()).select_from(GoogleSheetsConfig).where(GoogleSheetsConfig.active.is_(True)))),
        "last_success": _iso(next((x.finished_at for x in sync_runs if x.status == "completed"), None)),
        "inserted": sum(x.rows_inserted for x in sync_runs), "updated": sum(x.rows_updated for x in sync_runs),
        "skipped": sum(x.rows_skipped for x in sync_runs), "manual_changes": sum(x.manual_changes for x in sync_runs),
        "conflicts": sum(x.conflicts for x in sync_runs), "errors": sum(x.status == "failed" for x in sync_runs),
        "last_error": human_error(last_sync.error_message or "") if last_sync and last_sync.status == "failed" else None,
        "mapped_rows": _n(session.scalar(select(func.count()).select_from(SheetRowMapping))),
    }

    since_24 = now - timedelta(hours=24)
    def health_item(label: str, ok: bool, last: datetime | None, errors: int, problem: str, action: str) -> dict[str, Any]:
        return {"name": label, "status": "Работает" if ok else "Есть проблемы", "last_success": _iso(last),
                "errors_24h": errors, "problem": None if ok else problem, "action": action}
    source_health = {}
    for code, label in source_names.items():
        source_items = [x for x in session.scalars(select(SearchObservation).where(SearchObservation.source == code, SearchObservation.observed_at >= since_24))]
        last = session.scalar(select(func.max(SearchObservation.observed_at)).where(SearchObservation.source == code, SearchObservation.company_id.is_not(None)))
        last_error = session.scalar(select(func.max(SearchObservation.observed_at)).where(SearchObservation.source == code, SearchObservation.error_type.is_not(None)))
        errors = sum(x.error_type is not None for x in source_items)
        source_health[code] = health_item(label, bool(last) and (not last_error or last >= last_error), last, errors, "Источник временно недоступен.", "Повторить поиск")
    last_sent = session.scalar(select(func.max(EmailDelivery.sent_at)))
    last_reply = session.scalar(select(func.max(InboundReply.received_at)))
    last_run = session.scalar(select(func.max(DirectionRun.finished_at)).where(DirectionRun.status.in_(["completed", "exhausted"])))
    systems = [*source_health.values(),
        health_item("Google Таблица", sheets["configured"] and not sheets["last_error"], last_sync.finished_at if last_sync and last_sync.status == "completed" else None, sheets["errors"], "Последняя синхронизация завершилась с ошибкой.", "Проверить подключение"),
        health_item("Отправка почты", bool(session.scalar(select(func.count()).select_from(MailAccount).where(MailAccount.active.is_(True)))), last_sent, email_counts["errors"], "Проверьте подключение почтового ящика.", "Проверить почту"),
        health_item("Получение ответов", bool(session.scalar(select(func.count()).select_from(MailAccount).where(MailAccount.active.is_(True)))), last_reply, 0, "Не удалось проверить входящие письма.", "Проверить почту"),
        health_item("Поиск компаний", bool(last_run), last_run, len(search_errors), "Поиск завершился с ошибкой.", "Открыть направления"),
    ]

    recent_actions = []
    for item in sorted(result_observations, key=lambda x: x.observed_at, reverse=True)[:8]:
        recent_actions.append({"at": _iso(item.observed_at), "text": f"Найдена компания: {direction_names.get(item.direction_id, 'направление')}"})
    for item in sorted(deliveries, key=lambda x: x.created_at, reverse=True)[:5]:
        recent_actions.append({"at": _iso(item.created_at), "text": "Письмо отправлено" if item.sent_at else "Письмо поставлено в очередь"})
    recent_actions = sorted(recent_actions, key=lambda x: x["at"] or "", reverse=True)[:10]

    ai_conditions = [AIRequestLog.created_at >= start, AIRequestLog.created_at <= end]
    filtered_company_ids: set[str] | None = None
    if direction or city or source:
        filtered_stmt = select(Company.id)
        if direction:
            filtered_stmt = filtered_stmt.join(CompanyDirection).where(CompanyDirection.direction_id == direction)
        if city:
            filtered_stmt = filtered_stmt.where(Company.city == city)
        if source:
            filtered_stmt = filtered_stmt.where(Company.source == source)
        filtered_company_ids = set(session.scalars(filtered_stmt))
        ai_conditions.append(AIRequestLog.company_id.in_(filtered_company_ids or {""}))
    ai_logs = list(session.scalars(select(AIRequestLog).where(*ai_conditions)))
    actual_ai = [row for row in ai_logs if not row.cache_hit]
    ai_provenance_conditions = [
        CompanyFieldProvenance.discovery_method == "ai_website_analysis",
        CompanyFieldProvenance.discovered_at >= start, CompanyFieldProvenance.discovered_at <= end,
    ]
    if filtered_company_ids is not None:
        ai_provenance_conditions.append(CompanyFieldProvenance.company_id.in_(filtered_company_ids or {""}))
    ai_provenance = list(session.scalars(select(CompanyFieldProvenance).where(*ai_provenance_conditions)))
    ai_metrics = {
        "requests": len(actual_ai), "cache_hits": sum(row.cache_hit for row in ai_logs),
        "companies_enriched": len({row.company_id for row in ai_provenance}),
        "decision_makers_found": sum(row.field == "decision_maker_name" for row in ai_provenance),
        "emails_added": sum(row.field in {"company_email", "decision_maker_email"} for row in ai_provenance),
        "phones_added": sum(row.field in {"company_phone", "decision_maker_phone"} for row in ai_provenance),
        "personalized_emails": sum(row.operation == "email_personalization" and row.success for row in ai_logs),
        "input_tokens": sum(row.input_tokens for row in actual_ai),
        "cached_input_tokens": sum(row.cached_input_tokens for row in actual_ai),
        "output_tokens": sum(row.output_tokens for row in actual_ai),
        "reasoning_tokens": sum(row.reasoning_tokens for row in actual_ai),
        "estimated_cost": round(sum(row.estimated_cost for row in actual_ai), 6),
        "errors": sum(not row.success for row in actual_ai),
    }

    active_directions = _n(session.scalar(select(func.count()).select_from(Direction).where(Direction.active.is_(True), Direction.archived_at.is_(None))))
    active_mailboxes = _n(session.scalar(select(func.count()).select_from(MailAccount).where(MailAccount.active.is_(True))))
    ai_configured = bool(get_settings().deepseek_api_key)
    overall_ok = all(item["status"] == "Работает" for item in systems if item["name"] not in {"Яндекс Карты", "2ГИС"} or item["last_success"])

    return {
        "period": {"from": _iso(start), "to": _iso(end), "data_since": _iso(session.scalar(select(func.min(SearchObservation.observed_at))))},
        "filter_options": {
            "directions": [{"id": x.id, "name": x.name} for x in session.scalars(select(Direction).where(Direction.archived_at.is_(None)).order_by(Direction.name))],
            "cities": list(session.scalars(select(distinct(Company.city)).where(Company.city.is_not(None)).order_by(Company.city))),
            "sources": [{"id": key, "name": value} for key, value in source_names.items()],
            "mailboxes": [{"id": x.id, "name": x.name} for x in session.scalars(select(MailAccount).order_by(MailAccount.name))],
            "templates": [{"id": x.id, "name": x.name} for x in session.scalars(select(EmailTemplate).order_by(EmailTemplate.name))],
        },
        "status": {"ok": overall_ok, "label": "Система работает" if overall_ok else "Нужно обратить внимание"},
        "readiness": [
            {"label": "Направления настроены", "ready": active_directions > 0, "action": "Добавить направление"},
            {"label": "Почта подключена", "ready": active_mailboxes > 0, "action": "Подключить почту"},
            {"label": "Google Таблица подключена", "ready": sheets["configured"], "action": "Настроить Google Таблицу"},
            {"label": "Поиск компаний работает", "ready": bool(last_run), "action": "Запустить поиск"},
            {"label": "Умное дополнение данных подключено", "ready": ai_configured, "action": "Настроить дополнение"},
        ],
        "overview": {"found": len(result_observations), "new": sum(x.is_new for x in result_observations),
            "duplicates": sum(not x.is_new for x in result_observations), "companies": len(companies),
            "with_website": sum(bool(x.website) for x in companies), "with_email": sum(bool(x.company_email or x.email) for x in companies),
            "with_phone": sum(bool(x.company_phone or x.phone) for x in companies), "with_decision_maker": sum(bool(x.decision_maker_name) for x in companies),
            "active_directions": active_directions, **email_counts},
        "funnel": [{"label": "Найдено", "value": len(result_observations)},
            {"label": "Есть контакт", "value": sum(bool(x.company_email or x.email or x.company_phone or x.phone) for x in companies)},
            {"label": "Отправлено письмо", "value": sent}, {"label": "Открыто", "value": email_counts["opened"]},
            {"label": "Ответили", "value": email_counts["replied"]}],
        "search": {"runs": _n(session.scalar(select(func.count()).select_from(DirectionRun).where(DirectionRun.started_at >= start, DirectionRun.started_at <= end))),
            "successful": _n(session.scalar(select(func.count()).select_from(DirectionRun).where(DirectionRun.started_at >= start, DirectionRun.started_at <= end, DirectionRun.status.in_(["completed", "exhausted"])))),
            "failed": len({x.run_id for x in search_errors}), "observations": len(result_observations), "new": sum(x.is_new for x in result_observations),
            "duplicates": sum(not x.is_new for x in result_observations), "last_success": _iso(last_run),
            "last_error": human_error(search_errors[-1].error_message) if search_errors else None,
            "directions": sorted(direction_groups.values(), key=lambda x: (-x["new"], x["name"])),
            "errors": [{"at": _iso(x.observed_at), "source": source_names.get(x.source, x.source), "message": human_error(x.error_message or "")} for x in search_errors[-20:]]},
        "sources": {"rows": source_rows, "insights": insights}, "queries": grouped_observations("query"),
        "cities": grouped_observations("city"), "data_quality": {"total": len(companies), "fields": quality},
        "branches": {"known": len(branch_known), "unknown": len(companies) - len(branch_known),
            "average": round(sum(x.branches_count or 0 for x in branch_known) / len(branch_known), 1) if branch_known else 0,
            "networks": sum((x.branches_count or 0) > 1 for x in companies), "by_method": dict(branch_methods)},
        "websites": {"known": sum(bool(x.website) for x in companies), "unknown": sum(not bool(x.website) for x in companies),
            "coverage": _rate(sum(bool(x.website) for x in companies), len(companies)), "by_method": dict(website_methods)},
        "email": email_counts, "mailboxes": mailbox_rows, "templates": template_rows,
        "replies": {"total": len(reply_rows), "rows": reply_rows}, "sheets": sheets,
        "system": systems, "daily": daily, "recent_actions": recent_actions, "ai": ai_metrics,
    }
