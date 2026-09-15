"""Explicit production acceptance for one isolated test Company.

Run only with RUN_PRODUCTION_E2E=1. It sends one email to the already configured
test recipient, never discovers or selects a real customer.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    AIRequestLog, Campaign, Company, CompanyDirection, Direction, EmailDelivery,
    EmailEvent, EmailTemplate, GoogleSheetsConfig, MailAccount, Suppression,
)
from app.services.google_sheets import discover_schema, worksheet_from_config
from app.services.mailing import queue_campaign, send_delivery


TEST_SOURCE_ID = "proposal-e2e-restaurant-20260914"
TEMPLATE_NAME = "CONTRACT E2E — автоматическая NON-AI рассылка"


def sheet_state(session, config, direction, company_id):
    worksheet = worksheet_from_config(config, direction.sheet_tab)
    rows = worksheet.get_all_values()
    schema = discover_schema(rows)
    row_number = next(
        number for number, row in enumerate(rows[schema.data_row - 1 :], start=schema.data_row)
        if len(row) >= schema.leadflow_id_column and row[schema.leadflow_id_column - 1].strip() == company_id
    )
    row = rows[row_number - 1]
    value = lambda column: row[column - 1].strip() if column and len(row) >= column else ""
    return {
        "row": row_number,
        "leadflow_id_matches": value(schema.leadflow_id_column) == company_id,
        "status": value(schema.email_status_column),
        "sent_at": value(schema.sent_at_column),
        "template": value(schema.email_template_column),
        "mailbox": value(schema.mailbox_column),
        "interaction": value(schema.interaction_column),
    }


def run():
    if os.getenv("RUN_PRODUCTION_E2E") != "1":
        raise SystemExit("Set RUN_PRODUCTION_E2E=1 explicitly")
    with SessionLocal() as session:
        company = session.scalar(select(Company).where(Company.source_external_id == TEST_SOURCE_ID))
        link = session.scalar(select(CompanyDirection).where(CompanyDirection.company_id == company.id))
        direction = session.get(Direction, link.direction_id)
        mailbox = session.scalar(select(MailAccount).where(MailAccount.active.is_(True)).limit(1))
        config = session.scalar(select(GoogleSheetsConfig).where(GoogleSheetsConfig.active.is_(True)).limit(1))
        recipient = company.decision_maker_email or company.company_email or company.email
        if not all((company, direction, mailbox, config, recipient)):
            raise RuntimeError("Production test fixtures are incomplete")
        if "LeadFlow E2E" not in company.company_name:
            raise RuntimeError("Refusing to send for a non-test Company")
        suppression = session.get(Suppression, recipient.casefold())
        suppression_was_active = suppression.active if suppression else None
        original_city, original_mailbox_active = company.city, mailbox.active
        ai_before = session.scalar(select(func.count()).select_from(AIRequestLog)) or 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        try:
            if suppression:
                suppression.active = False
            template = EmailTemplate(
                name=f"{TEMPLATE_NAME} {stamp}", direction_id=direction.id,
                subject_template="{{ company_name }} — тест автоматического шаблона",
                html_template='<p>Здравствуйте, {{ company_name }}!</p><p>Это безопасная автоматическая проверка LeadFlow.</p><a href="https://www.bogorodsk-pryanik.ru/">Открыть сайт</a>',
                text_template="Здравствуйте, {{ company_name }}! Это безопасная автоматическая проверка LeadFlow.",
                active=True,
            )
            session.add(template); session.flush()
            error_campaign = Campaign(
                name=f"CONTRACT E2E send_error {stamp}", direction_id=direction.id,
                mailbox_id=mailbox.id, template_id=template.id, daily_limit=1, run_limit=1,
                sending_interval_seconds=1, cooldown_days=0, status="paused", active=False,
            )
            session.add(error_campaign); session.flush()
            error_delivery = EmailDelivery(
                campaign_id=error_campaign.id, direction_id=direction.id, company_id=company.id,
                mailbox_id=mailbox.id, template_id=template.id, recipient_email=recipient,
                subject=f"{company.company_name} — controlled send_error", html_body="<p>Не отправляется</p>",
                text_body="Не отправляется", send_mode="campaign", status="queued",
                tracking_token=f"contract-error-{stamp}", unsubscribe_token=f"contract-error-unsub-{stamp}",
            )
            session.add(error_delivery); session.flush()
            from app.services.email_events import record_email_event
            record_email_event(session, error_delivery, "queued", {"acceptance": True})
            session.commit()
            mailbox.active = False; session.commit()
            send_delivery(session, error_delivery, mailbox)
            error_sheet = sheet_state(session, config, direction, company.id)
            mailbox.active = True; session.commit()

            unique_city = f"CONTRACT-E2E-{stamp}"
            company.city = unique_city; session.commit()
            campaign = Campaign(
                name=f"CONTRACT E2E automatic {stamp}", direction_id=direction.id,
                city=unique_city, mailbox_id=mailbox.id, template_id=template.id,
                daily_limit=1, run_limit=1, sending_interval_seconds=1, cooldown_days=0,
                status="running", active=False,
            )
            session.add(campaign); session.commit()
            queue_result = queue_campaign(session, campaign, get_settings())
            delivery = session.scalar(select(EmailDelivery).where(EmailDelivery.campaign_id == campaign.id))
            company.city = original_city; session.commit()
            if queue_result["queued"] != 1 or not delivery:
                raise RuntimeError(f"Expected one isolated delivery, got {queue_result}")
            send_delivery(session, delivery, mailbox)
            session.refresh(delivery)
            sent_sheet = sheet_state(session, config, direction, company.id)
            ai_after = session.scalar(select(func.count()).select_from(AIRequestLog)) or 0
            template.active = False; campaign.status = "completed"; session.commit()
            result = {
                "company_id": company.id,
                "delivery_id": delivery.id,
                "error_delivery_id": error_delivery.id,
                "send_error_sheet": error_sheet,
                "automatic_queue": queue_result,
                "automatic_status": delivery.status,
                "direction_template": delivery.template_id == template.id and template.direction_id == direction.id,
                "company_in_subject": company.company_name in delivery.subject,
                "company_in_html": company.company_name in delivery.html_body,
                "non_ai": ai_after == ai_before,
                "sent_sheet": sent_sheet,
                "event_types": list(session.scalars(select(EmailEvent.event_type).where(
                    EmailEvent.delivery_id.in_([error_delivery.id, delivery.id])
                ).order_by(EmailEvent.occurred_at))),
            }
            print(json.dumps(result, ensure_ascii=False))
        finally:
            company.city = original_city
            mailbox.active = original_mailbox_active
            if suppression:
                suppression.active = suppression_was_active
            session.commit()


if __name__ == "__main__":
    run()
