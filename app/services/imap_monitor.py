from __future__ import annotations

import email
import imaplib
import smtplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from email.message import EmailMessage

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import EmailDelivery, InboundReply, MailAccount, Suppression
from app.services.secrets import decrypt_secret


def _forward(account: MailAccount, raw_message: bytes, manager_email: str) -> None:
    message = EmailMessage()
    message["Subject"] = "LeadFlow: получен ответ"
    message["From"] = account.from_email
    message["To"] = manager_email
    message.set_content("Получен ответ на рассылку LeadFlow. Исходное письмо приложено.")
    message.add_attachment(raw_message, maintype="message", subtype="rfc822", filename="reply.eml")
    if account.smtp_security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30)
    else:
        smtp = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
    with smtp:
        if account.smtp_security == "starttls":
            smtp.starttls()
        smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted))
        smtp.send_message(message)


def _text(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in (part.get("Content-Disposition") or ""):
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    return (message.get_payload(decode=True) or b"").decode(message.get_content_charset() or "utf-8", errors="replace")


def poll_mailbox(session: Session, account: MailAccount, manager_email: str | None = None) -> int:
    password = decrypt_secret(account.imap_password_encrypted)
    with imaplib.IMAP4_SSL(account.imap_host, account.imap_port) as client:
        client.login(account.imap_login, password)
        client.select("INBOX")
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            return 0
        saved = 0
        for uid in data[0].split():
            status, payload = client.fetch(uid, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1])
            message_id = message.get("Message-ID")
            if message_id and session.scalar(select(InboundReply.id).where(InboundReply.message_id == message_id)):
                continue
            references = " ".join([message.get("In-Reply-To") or "", message.get("References") or ""])
            sender = parseaddr(message.get("From") or "")[1].casefold()
            delivery = session.scalar(
                select(EmailDelivery).where(
                    or_(
                        EmailDelivery.message_id == message.get("In-Reply-To"),
                        EmailDelivery.recipient_email == sender,
                    )
                ).order_by(EmailDelivery.sent_at.desc()).limit(1)
            )
            if not delivery and references:
                for candidate in session.scalars(select(EmailDelivery).where(EmailDelivery.message_id.is_not(None))):
                    if candidate.message_id in references:
                        delivery = candidate
                        break
            subject = str(make_header(decode_header(message.get("Subject") or "")))
            inbound = InboundReply(
                delivery_id=delivery.id if delivery else None,
                mailbox_id=account.id,
                message_id=message_id,
                sender=sender,
                subject=subject,
                text_body=_text(message),
            )
            session.add(inbound)
            if delivery:
                delivery.status = "replied"
                delivery.replied_at = datetime.now(timezone.utc)
            # Standard DSN heuristics; no attempt is made to bypass server policy.
            if delivery and ("mailer-daemon" in sender or "delivery status notification" in subject.casefold()):
                delivery.status = "bounce"
                delivery.bounced_at = datetime.now(timezone.utc)
                session.merge(Suppression(email=delivery.recipient_email, reason="bounce"))
            session.commit()
            if manager_email:
                try:
                    _forward(account, payload[0][1], manager_email)
                except Exception:
                    # The reply is already persisted; forwarding can be retried
                    # operationally without losing the inbound message.
                    pass
            saved += 1
        return saved
