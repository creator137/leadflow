from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import MailAccount
from app.services.secrets import encrypt_secret
from app.services.sheet_personalization import _default_mailbox


def mailbox(email: str, *, primary: bool) -> MailAccount:
    return MailAccount(
        name=email, from_email=email, smtp_host="smtp.example.test", smtp_login=email,
        smtp_password_encrypted=encrypt_secret("secret"), imap_host="imap.example.test",
        imap_login=email, imap_password_encrypted=encrypt_secret("secret"),
        active=True, is_primary=primary,
    )


def test_explicit_primary_mailbox_wins_over_creation_order() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        development = mailbox("development@example.test", primary=False)
        production = mailbox("production@example.test", primary=True)
        session.add_all([development, production]); session.commit()
        assert _default_mailbox(session).id == production.id
