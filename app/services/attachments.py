from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DirectionAttachment


MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_DIRECTION_BYTES = 18 * 1024 * 1024
MAX_DIRECTION_FILES = 12
ALLOWED = {
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}


def safe_filename(value: str) -> str:
    name = Path(value or "file").name.strip()
    name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._() -]+", "_", name)
    return name[:255] or "file"


def attachment_dict(row: DirectionAttachment) -> dict[str, Any]:
    return {
        "id": row.id, "filename": row.filename, "storage_name": row.storage_name,
        "content_type": row.content_type, "size_bytes": row.size_bytes,
        "sha256": row.sha256, "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def active_attachments(session: Session, direction_id: str) -> list[dict[str, Any]]:
    return [attachment_dict(row) for row in session.scalars(select(DirectionAttachment).where(
        DirectionAttachment.direction_id == direction_id, DirectionAttachment.active.is_(True),
    ).order_by(DirectionAttachment.created_at))]


async def save_attachment(
    session: Session, settings: Settings, direction_id: str, upload: UploadFile,
) -> DirectionAttachment:
    filename = safe_filename(upload.filename or "")
    suffix = Path(filename).suffix.casefold()
    if suffix not in ALLOWED:
        raise ValueError("Можно прикреплять PDF, PowerPoint и изображения JPG, PNG или WEBP.")
    content = await upload.read(MAX_FILE_BYTES + 1)
    if not content:
        raise ValueError("Файл пустой.")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Один файл должен быть не больше 12 МБ.")
    existing = list(session.scalars(select(DirectionAttachment).where(
        DirectionAttachment.direction_id == direction_id, DirectionAttachment.active.is_(True),
    )))
    if len(existing) >= MAX_DIRECTION_FILES:
        raise ValueError("Для одного направления можно прикрепить не больше 12 файлов.")
    if sum(x.size_bytes for x in existing) + len(content) > MAX_DIRECTION_BYTES:
        raise ValueError("Общий размер файлов направления должен быть не больше 18 МБ, чтобы почтовый сервер принял письмо.")
    digest = hashlib.sha256(content).hexdigest()
    duplicate = session.scalar(select(DirectionAttachment).where(
        DirectionAttachment.direction_id == direction_id, DirectionAttachment.sha256 == digest,
    ))
    if duplicate:
        if not duplicate.active:
            duplicate.active = True; session.commit(); session.refresh(duplicate)
        return duplicate
    storage_name = f"{direction_id}_{digest}{suffix}"
    directory = Path(settings.attachment_storage_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / storage_name
    if not target.exists():
        target.write_bytes(content)
    row = DirectionAttachment(
        direction_id=direction_id, filename=filename, storage_name=storage_name,
        content_type=ALLOWED[suffix], size_bytes=len(content), sha256=digest,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return session.scalar(select(DirectionAttachment).where(
            DirectionAttachment.direction_id == direction_id, DirectionAttachment.sha256 == digest,
        ))
    session.refresh(row)
    return row


def attachment_path(settings: Settings, storage_name: str) -> Path:
    directory = Path(settings.attachment_storage_dir).resolve()
    candidate = (directory / Path(storage_name).name).resolve()
    if candidate.parent != directory:
        raise ValueError("Некорректный путь файла.")
    return candidate
