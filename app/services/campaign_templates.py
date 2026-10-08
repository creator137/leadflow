from __future__ import annotations

import html
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Direction, DirectionProposalTemplate, EmailTemplate, SenderSettings


def proposal_email_text(template: DirectionProposalTemplate, fallback_signature: str = "") -> str:
    """Build the non-personalized campaign copy from the direction's canonical proposal."""
    return "\n\n".join(
        value.strip()
        for value in (
            template.greeting,
            template.main_body,
            template.extra_block,
            template.cta,
            template.signature or fallback_signature,
        )
        if value and value.strip()
    )


def sync_direction_campaign_template(
    session: Session, direction: Direction,
) -> EmailTemplate | None:
    """Mirror the canonical proposal into the transport template used by campaigns."""
    proposal = session.scalar(select(DirectionProposalTemplate).where(
        DirectionProposalTemplate.direction_id == direction.id,
    ))
    if not proposal:
        return None
    transport = session.get(EmailTemplate, direction.automatic_template_id) if direction.automatic_template_id else None
    if not transport:
        transport = EmailTemplate(
            name=f"Шаблон КП — {direction.name}",
            direction_id=direction.id,
            subject_template=proposal.subject,
            html_template="",
            text_template="",
            active=True,
        )
        session.add(transport)
        session.flush()
        direction.automatic_template_id = transport.id
    sender = session.get(SenderSettings, "default")
    text = proposal_email_text(proposal, sender.signature_text if sender else "")
    escaped = html.escape(text).replace("\n", "<br>")
    escaped = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"{{ \1 }}", escaped)
    transport.name = f"Шаблон КП — {direction.name}"
    transport.direction_id = direction.id
    transport.subject_template = proposal.subject
    transport.text_template = text
    transport.html_template = f'<div style="font-family:Arial,sans-serif;font-size:16px;line-height:1.55">{escaped}</div>'
    transport.active = True
    return transport
