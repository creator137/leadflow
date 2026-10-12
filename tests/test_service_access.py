from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ServiceAccessCredential
from app.services.service_access import ensure_service_access, serialize_service_access, update_service_access


def test_service_access_defaults_and_encrypted_updates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        rows = ensure_service_access(session)
        assert [row.slug for row in rows] == ["beget", "deepseek-top-up", "deepseek-platform"]
        assert rows[1].login_encrypted is None and rows[1].password_encrypted is None

        beget = session.scalar(select(ServiceAccessCredential).where(ServiceAccessCredential.slug == "beget"))
        update_service_access(
            session, beget, login="billing-user", password="private-password",
            update_login=True, update_password=True,
        )
        assert beget.login_encrypted != "billing-user"
        assert beget.password_encrypted != "private-password"
        assert serialize_service_access(beget)["login"] == "billing-user"
        assert serialize_service_access(beget)["password"] == "private-password"

        update_service_access(session, beget, login="", password="", update_login=True, update_password=True)
        assert beget.login_encrypted is None and beget.password_encrypted is None


def test_help_has_access_section_without_embedded_credentials() -> None:
    root = Path(__file__).parents[1]
    help_script = (root / "app/static/help.js").read_text()
    service_source = (root / "app/services/service_access.py").read_text()
    assert "Доступы" in help_script
    assert "Пополнение ИИ" in service_source
    assert "/api/service-access" in help_script
    assert "type=\"password\"" in help_script
    assert "billing-user" not in help_script
    assert "private-password" not in help_script
