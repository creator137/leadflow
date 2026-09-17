from __future__ import annotations

import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DirectionAttachment


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_DIRECTION_BYTES = 20 * 1024 * 1024
MAX_DIRECTION_FILES = 12
GENERIC_MIME_TYPES = {"", "application/octet-stream", "application/zip", "application/x-zip-compressed"}
DANGEROUS_EXTENSIONS = {
    ".app", ".bat", ".bin", ".cmd", ".com", ".cpl", ".dll", ".dmg", ".exe",
    ".hta", ".jar", ".js", ".jse", ".msi", ".php", ".ps1", ".py", ".scr",
    ".sh", ".vb", ".vbe", ".vbs", ".wsf",
}
ALLOWED: dict[str, dict[str, Any]] = {
    ".jpg": {"mime": "image/jpeg", "accepted": {"image/jpeg", "image/jpg"}, "kind": "jpg"},
    ".jpeg": {"mime": "image/jpeg", "accepted": {"image/jpeg", "image/jpg"}, "kind": "jpg"},
    ".png": {"mime": "image/png", "accepted": {"image/png"}, "kind": "png"},
    ".webp": {"mime": "image/webp", "accepted": {"image/webp"}, "kind": "webp"},
    ".pdf": {"mime": "application/pdf", "accepted": {"application/pdf"}, "kind": "pdf"},
    ".doc": {"mime": "application/msword", "accepted": {"application/msword", "application/x-ole-storage"}, "kind": "doc"},
    ".docx": {"mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "accepted": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}, "kind": "docx"},
    ".xls": {"mime": "application/vnd.ms-excel", "accepted": {"application/vnd.ms-excel", "application/x-ole-storage"}, "kind": "xls"},
    ".xlsx": {"mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "accepted": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}, "kind": "xlsx"},
    ".ppt": {"mime": "application/vnd.ms-powerpoint", "accepted": {"application/vnd.ms-powerpoint", "application/x-ole-storage"}, "kind": "ppt"},
    ".pptx": {"mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "accepted": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"}, "kind": "pptx"},
    ".txt": {"mime": "text/plain", "accepted": {"text/plain"}, "kind": "text"},
    ".csv": {"mime": "text/csv", "accepted": {"text/csv", "application/csv", "application/vnd.ms-excel", "text/plain"}, "kind": "text"},
}


def safe_filename(value: str) -> str:
    name = Path(value or "file").name.strip()
    name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._() -]+", "_", name)
    return name[:255] or "file"


def _validated_filename(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw or "\x00" in raw:
        raise ValueError("Некорректное имя файла.")
    filename = safe_filename(raw)
    suffixes = [item.casefold() for item in Path(filename).suffixes]
    if not suffixes or suffixes[-1] not in ALLOWED or any(item in DANGEROUS_EXTENSIONS for item in suffixes):
        raise ValueError("Формат файла не поддерживается.")
    return filename, suffixes[-1]


def _valid_zip_package(content: bytes, folder: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or not any(name.startswith(folder) for name in names):
                return False
            total_uncompressed = 0
            for info in archive.infolist():
                total_uncompressed += info.file_size
                if info.file_size > MAX_FILE_BYTES or ".." in Path(info.filename).parts:
                    return False
            if total_uncompressed > MAX_FILE_BYTES:
                return False
            return archive.testzip() is None
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False


def _valid_text(content: bytes) -> bool:
    if b"\x00" in content:
        return False
    try:
        value = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            value = content.decode("cp1251")
        except UnicodeDecodeError:
            return False
    if not value:
        return False
    controls = sum(ord(char) < 32 and char not in "\r\n\t" for char in value)
    return controls / max(1, len(value)) < 0.01


def _content_matches(kind: str, content: bytes) -> bool:
    if kind == "jpg":
        return content.startswith(b"\xff\xd8\xff")
    if kind == "png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if kind == "pdf":
        return content.startswith(b"%PDF-") and b"%%EOF" in content[-2048:]
    if kind in {"docx", "xlsx", "pptx"}:
        return _valid_zip_package(content, {"docx": "word/", "xlsx": "xl/", "pptx": "ppt/"}[kind])
    if kind in {"doc", "xls", "ppt"}:
        if not content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return False
        markers = {
            "doc": (b"WordDocument", "WordDocument".encode("utf-16le")),
            "xls": (b"Workbook", "Workbook".encode("utf-16le"), b"Book", "Book".encode("utf-16le")),
            "ppt": (b"PowerPoint Document", "PowerPoint Document".encode("utf-16le")),
        }
        return any(marker in content for marker in markers[kind])
    if kind == "text":
        return _valid_text(content)
    return False


def _validate_content_type(suffix: str, declared: str | None) -> None:
    content_type = (declared or "").split(";", 1)[0].strip().casefold()
    accepted = set(ALLOWED[suffix]["accepted"]) | GENERIC_MIME_TYPES
    if content_type not in accepted:
        raise ValueError("Содержимое файла не соответствует выбранному формату.")


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
    filename, suffix = _validated_filename(upload.filename or "")
    _validate_content_type(suffix, upload.content_type)
    content = await upload.read(MAX_FILE_BYTES + 1)
    if not content:
        raise ValueError("Файл пустой.")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError("Файл слишком большой. Максимальный размер — 20 МБ.")
    if not _content_matches(str(ALLOWED[suffix]["kind"]), content):
        raise ValueError("Содержимое файла не соответствует выбранному формату.")
    existing = list(session.scalars(select(DirectionAttachment).where(
        DirectionAttachment.direction_id == direction_id, DirectionAttachment.active.is_(True),
    )))
    if len(existing) >= MAX_DIRECTION_FILES:
        raise ValueError("Для одного направления можно прикрепить не больше 12 файлов.")
    if sum(x.size_bytes for x in existing) + len(content) > MAX_DIRECTION_BYTES:
        raise ValueError("Общий размер вложений направления должен быть не больше 20 МБ.")
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
        content_type=str(ALLOWED[suffix]["mime"]), size_bytes=len(content), sha256=digest,
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
