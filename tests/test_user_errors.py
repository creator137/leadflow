import imaplib
import smtplib

from app.services.user_errors import human_error


def test_human_readable_errors_hide_technical_classes() -> None:
    assert human_error(smtplib.SMTPAuthenticationError(535, b"bad")) == "Не удалось войти в почту. Проверьте адрес и пароль приложения."
    assert human_error(imaplib.IMAP4.error("bad")) == "Не удалось проверить входящие письма."
    assert "403" not in human_error("Google API HTTP 403")
    assert "Traceback" not in human_error("Traceback: KeyError")
