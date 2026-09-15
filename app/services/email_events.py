from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import EmailDelivery, EmailEvent

logger = logging.getLogger(__name__)


def record_email_event(
    session: Session, delivery: EmailDelivery, event_type: str, details: dict[str, Any] | None = None,
) -> EmailEvent:
    event = EmailEvent(
        delivery_id=delivery.id,
        company_id=delivery.company_id,
        event_type=event_type,
        details=details or {},
    )
    session.add(event)
    return event


def sync_email_event_to_sheets(session: Session, delivery: EmailDelivery) -> bool:
    """Best-effort write-back. Email tracking must survive a temporary Sheets outage."""
    try:
        from app.services.google_sheets import sync_delivery_tracking_to_sheet

        return sync_delivery_tracking_to_sheet(session, delivery)
    except Exception:
        session.rollback()
        logger.exception("Google Sheets email tracking write-back failed for delivery %s", delivery.id)
        return False
