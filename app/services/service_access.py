from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ServiceAccessCredential
from app.services.secrets import decrypt_secret, encrypt_secret


DEFAULT_SERVICES = (
    {
        "slug": "beget",
        "name": "Beget",
        "purpose": "Пополнение сервера",
        "url": "https://cp.beget.com/pay",
        "note": "Используйте для проверки баланса и своевременной оплаты сервера LeadFlow.",
        "sort_order": 10,
    },
    {
        "slug": "deepseek-top-up",
        "name": "Пополнение ИИ",
        "purpose": "Покупка пополнения DeepSeek",
        "url": "https://playerok.com/deepseek/top-up",
        "note": "Логин и пароль можно заполнить после регистрации или покупки.",
        "sort_order": 20,
    },
    {
        "slug": "deepseek-platform",
        "name": "DeepSeek API",
        "purpose": "Ключи API и контроль расходов ИИ",
        "url": "https://platform.deepseek.com",
        "note": "Здесь находятся ключи API, баланс и история расходов DeepSeek.",
        "sort_order": 30,
    },
)


def ensure_service_access(session: Session) -> list[ServiceAccessCredential]:
    existing = {row.slug: row for row in session.scalars(select(ServiceAccessCredential))}
    changed = False
    for definition in DEFAULT_SERVICES:
        row = existing.get(definition["slug"])
        if row is None:
            row = ServiceAccessCredential(**definition)
            session.add(row)
            existing[row.slug] = row
            changed = True
        else:
            for field in ("name", "purpose", "url", "note", "sort_order"):
                value = definition[field]
                if getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True
    if changed:
        session.commit()
    return sorted(existing.values(), key=lambda row: (row.sort_order, row.name))


def serialize_service_access(row: ServiceAccessCredential) -> dict[str, object]:
    return {
        "slug": row.slug,
        "name": row.name,
        "purpose": row.purpose,
        "url": row.url,
        "login": decrypt_secret(row.login_encrypted) if row.login_encrypted else "",
        "password": decrypt_secret(row.password_encrypted) if row.password_encrypted else "",
        "note": row.note,
        "updated_at": row.updated_at,
    }


def update_service_access(
    session: Session, row: ServiceAccessCredential, *, login: str | None = None,
    password: str | None = None, update_login: bool = False, update_password: bool = False,
) -> ServiceAccessCredential:
    if update_login:
        row.login_encrypted = encrypt_secret(login.strip()) if login and login.strip() else None
    if update_password:
        row.password_encrypted = encrypt_secret(password) if password else None
    session.commit()
    session.refresh(row)
    return row
