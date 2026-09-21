from __future__ import annotations

import email
import logging
import re
import smtplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Company, Direction, EmailDelivery, ImapFolderCheckpoint, InboundReply,
    MailAccount, SheetRowMapping, Suppression,
)
from app.services.mailing import imap_connection, normalize_email, smtp_connection
from app.services.email_events import record_email_event, sync_email_event_to_sheets
from app.services.secrets import decrypt_secret

STATUS_RE = re.compile(r"(?:^|\s)([245])\.\d\.\d(?:\s|$)")
FOLDER_RE = re.compile(r'^\((?P<attributes>[^)]*)\)\s+"[^"]*"\s+(?:"(?P<quoted>[^"]+)"|(?P<bare>\S+))\s*$')
logger = logging.getLogger(__name__)


def _reply_context(session: Session, delivery: EmailDelivery, inbound: InboundReply) -> str:
    company = session.get(Company, delivery.company_id)
    direction = session.get(Direction, delivery.direction_id) if delivery.direction_id else None
    mapping = session.scalar(select(SheetRowMapping).where(
        SheetRowMapping.company_id == delivery.company_id,
        SheetRowMapping.direction_id == delivery.direction_id,
    ).order_by(SheetRowMapping.last_synced_at.desc()).limit(1))
    return "\n".join((
        "Получен ответ на КП LeadFlow.",
        f"Направление / вкладка: {(direction.name if direction else '—')} / {(mapping.sheet_tab if mapping else '—')}",
        f"Компания: {(company.company_name if company else '—')}",
        f"Строка Google Sheets: {(mapping.sheet_row if mapping else '—')}",
        f"LeadFlow ID: {delivery.company_id}",
        f"Email клиента: {inbound.sender}",
        f"Тема исходного КП: {delivery.subject}",
        "",
        "Текст ответа клиента:",
        inbound.text_body or "(текстовая часть ответа отсутствует)",
        "",
        "Оригинальное письмо приложено файлом reply.eml.",
    ))


def _forward(session: Session, account: MailAccount, raw_message: bytes, manager_email: str, delivery: EmailDelivery, inbound: InboundReply) -> None:
    message = EmailMessage()
    company = session.get(Company, delivery.company_id)
    message["Subject"], message["From"], message["To"] = f"LeadFlow: ответ — {(company.company_name if company else 'компания')}", account.from_email, manager_email
    message.set_content(_reply_context(session, delivery, inbound))
    message.add_attachment(raw_message, maintype="message", subtype="rfc822", filename="reply.eml")
    with smtp_connection(account) as smtp:
        smtp.login(account.smtp_login, decrypt_secret(account.smtp_password_encrypted)); smtp.send_message(message)


def _text(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in (part.get("Content-Disposition") or ""):
                return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    return (message.get_payload(decode=True) or b"").decode(message.get_content_charset() or "utf-8", errors="replace")


def classify_bounce(message: email.message.Message, sender: str, subject: str, body: str) -> str | None:
    is_dsn = message.get_content_type() == "multipart/report" or "mailer-daemon" in sender or "delivery status notification" in subject.casefold()
    if not is_dsn: return None
    diagnostic = " ".join([message.get("Status") or "", message.get("Diagnostic-Code") or "", body[:5000]])
    match = STATUS_RE.search(diagnostic)
    if match and match.group(1) == "5": return "hard_bounce"
    if match and match.group(1) == "4": return "soft_bounce"
    return "unknown_bounce"


def _match_delivery(session: Session, message: email.message.Message, sender: str, raw: bytes) -> EmailDelivery | None:
    in_reply_to, references = message.get("In-Reply-To") or "", message.get("References") or ""
    ids = re.findall(r"<[^>]+>", f"{in_reply_to} {references}")
    if ids:
        matched = session.scalar(select(EmailDelivery).where(EmailDelivery.message_id.in_(ids)).order_by(EmailDelivery.sent_at.desc()).limit(1))
        if matched: return matched
    leadflow_id = message.get("X-LeadFlow-ID")
    if leadflow_id:
        matched = session.get(EmailDelivery, leadflow_id)
        if matched: return matched
    raw_ids = set(re.findall(r"<[^>\s]+@[^>\s]+>", raw.decode("utf-8", errors="ignore")))
    if raw_ids:
        matched = session.scalar(select(EmailDelivery).where(EmailDelivery.message_id.in_(raw_ids)).order_by(EmailDelivery.sent_at.desc()).limit(1))
        if matched: return matched
    return session.scalar(select(EmailDelivery).where(EmailDelivery.recipient_email == sender, EmailDelivery.sent_at.is_not(None)).order_by(EmailDelivery.sent_at.desc()).limit(1))


def process_inbound(
    session: Session, account: MailAccount, raw: bytes, uid: int, uidvalidity: int,
    manager_email: str | None = None, folder: str = "INBOX",
) -> bool:
    if session.scalar(select(InboundReply.id).where(
        InboundReply.mailbox_id == account.id, InboundReply.imap_folder == folder,
        InboundReply.imap_uidvalidity == uidvalidity, InboundReply.imap_uid == uid,
    )):
        return False
    message = email.message_from_bytes(raw)
    message_id = message.get("Message-ID")
    if message_id and session.scalar(select(InboundReply.id).where(InboundReply.message_id == message_id)): return False
    sender = normalize_email(parseaddr(message.get("From") or "")[1])
    subject = str(make_header(decode_header(message.get("Subject") or "")))
    body = _text(message)
    delivery = _match_delivery(session, message, sender, raw)
    bounce_type = classify_bounce(message, sender, subject, body)
    # A newsletter, service notice or arbitrary incoming message must never be
    # shown as a client reply or forwarded to a manager.
    if not delivery and not bounce_type:
        return False
    inbound = InboundReply(
        delivery_id=delivery.id if delivery else None, mailbox_id=account.id, message_id=message_id,
        imap_uid=uid, imap_uidvalidity=uidvalidity, imap_folder=folder,
        in_reply_to=message.get("In-Reply-To"), references=message.get("References"),
        sender=sender or "unknown", subject=subject, text_body=body, bounce_type=bounce_type,
    )
    session.add(inbound)
    if delivery and bounce_type:
        delivery.bounced_at = datetime.now(timezone.utc)
        delivery.status = "bounced"
        record_email_event(session, delivery, "bounced", {"reason": bounce_type})
        if bounce_type == "hard_bounce":
            session.merge(Suppression(email=normalize_email(delivery.recipient_email), reason="hard_bounce", source_delivery_id=delivery.id, active=True))
    elif delivery:
        delivery.status, delivery.replied_at = "replied", datetime.now(timezone.utc)
        record_email_event(session, delivery, "replied")
        company = session.get(Company, delivery.company_id)
        if company:
            company.action, company.result = "Получен ответ", "Есть ответ"
    session.commit()
    if delivery:
        sync_email_event_to_sheets(session, delivery)
    forward_to = account.forward_replies_to or manager_email
    if forward_to and delivery and not bounce_type:
        try:
            _forward(session, account, raw, forward_to, delivery, inbound)
            inbound.forwarded_at = datetime.now(timezone.utc); session.commit()
        except (OSError, smtplib.SMTPException):
            logger.exception("Manager reply forwarding failed for inbound %s", inbound.id)
    return True


def _junk_folders(client) -> list[str]:
    status, items = client.list()
    if status != "OK": return []
    folders: list[str] = []
    for item in items or []:
        value = item.decode("utf-8", errors="replace") if isinstance(item, bytes) else str(item)
        match = FOLDER_RE.match(value)
        if not match: continue
        attributes = match.group("attributes").casefold()
        name = match.group("quoted") or match.group("bare") or ""
        if "\\junk" in attributes or name.casefold() in {"spam", "junk"}:
            folders.append(name)
    return list(dict.fromkeys(folders))


def _folder_checkpoint(session: Session, account: MailAccount, folder: str, uidvalidity: int) -> ImapFolderCheckpoint:
    checkpoint = session.scalar(select(ImapFolderCheckpoint).where(
        ImapFolderCheckpoint.mailbox_id == account.id, ImapFolderCheckpoint.folder == folder,
    ))
    if checkpoint is None:
        checkpoint = ImapFolderCheckpoint(mailbox_id=account.id, folder=folder, uidvalidity=uidvalidity, last_uid=0)
        session.add(checkpoint); session.flush()
    elif checkpoint.uidvalidity != uidvalidity:
        checkpoint.uidvalidity, checkpoint.last_uid = uidvalidity, 0
    return checkpoint


def _poll_folder(session: Session, client, account: MailAccount, folder: str, manager_email: str | None) -> int:
    status, _ = client.select(folder)
    if status != "OK": return 0
    response = client.response("UIDVALIDITY")[1]
    uidvalidity = int(response[0]) if response and response[0] else 0
    if folder == "INBOX":
        if account.imap_uidvalidity != uidvalidity:
            account.imap_uidvalidity, account.imap_last_uid = uidvalidity, 0
        last_uid = account.imap_last_uid
    else:
        checkpoint = _folder_checkpoint(session, account, folder, uidvalidity)
        last_uid = checkpoint.last_uid
    session.commit()
    status, data = client.uid("search", None, f"UID {last_uid + 1}:*")
    if status != "OK": return 0
    saved = 0
    for value in data[0].split():
        uid = int(value)
        status, payload = client.uid("fetch", value, "(RFC822)")
        if status == "OK" and payload and isinstance(payload[0], tuple):
            saved += int(process_inbound(session, account, payload[0][1], uid, uidvalidity, manager_email, folder))
        if folder == "INBOX": account.imap_last_uid = max(account.imap_last_uid, uid)
        else: checkpoint.last_uid = max(checkpoint.last_uid, uid)
        session.commit()
    return saved


def poll_mailbox(session: Session, account: MailAccount, manager_email: str | None = None) -> int:
    with imap_connection(account) as client:
        client.login(account.imap_login, decrypt_secret(account.imap_password_encrypted))
        return _poll_folder(session, client, account, "INBOX", manager_email) + sum(
            _poll_folder(session, client, account, folder, manager_email) for folder in _junk_folders(client)
        )
