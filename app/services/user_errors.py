from __future__ import annotations

import imaplib
import smtplib


def human_error(exc: Exception | str) -> str:
    """Map technical failures to safe, actionable Russian messages."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Не удалось войти в почту. Проверьте адрес и пароль приложения."
    if isinstance(exc, imaplib.IMAP4.error):
        return "Не удалось проверить входящие письма."
    text = str(exc).casefold()
    if "suppressed" in text:
        return "На этот адрес запрещена отправка."
    if "no recipient email" in text:
        return "У компании не указан email."
    if "cooldown" in text or "already sent" in text:
        return "Этому получателю недавно уже отправляли письмо."
    if "mailbox daily limit" in text or "daily limit" in text:
        return "Дневной лимит писем с этого адреса достигнут."
    if any(word in text for word in ("google", "spreadsheet", "worksheet", "gspread", "403")):
        return "Нет доступа к Google Таблице. Проверьте доступ для сервисного адреса."
    if any(word in text for word in ("smtpauthentication", "authentication failed", "invalid credentials")):
        return "Не удалось войти в почту. Проверьте адрес и пароль приложения."
    if "imap" in text:
        return "Не удалось проверить входящие письма."
    if any(word in text for word in ("captcha", "sourceblocked", "parser", "2gis", "yandex")):
        return "Источник поиска временно недоступен. Система попробует ещё раз."
    if any(word in text for word in ("ai", "provider", "api key")):
        return "Умное дополнение данных пока не подключено."
    return "Не удалось выполнить действие. Попробуйте ещё раз."


STATUS_RU = {
    "queued": "В очереди", "sending": "Отправляется", "sent": "Отправлено",
    "replied": "Получен ответ", "bounced": "Не доставлено", "opened": "Открыто",
    "clicked": "Перешли по ссылке", "unsubscribed": "Отписались",
    "failed": "Ошибка", "send_error": "Ошибка", "paused": "Приостановлено",
    "running": "Работает", "completed": "Завершено", "exhausted": "Завершено",
    "blocked": "Нужна проверка",
}
