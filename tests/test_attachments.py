from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.config import Settings
from app.db import Base
from app.models import Direction, EmailDelivery, MailAccount
from app.services.attachments import MAX_FILE_BYTES, active_attachments, attachment_path, save_attachment
from app.services.mailing import build_email_message


MIMES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "pdf": "application/pdf", "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain", "csv": "text/csv",
}


def office_zip(folder: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr(f"{folder}/document.xml", "<document>LeadFlow test</document>")
    return output.getvalue()


def fixture_bytes(extension: str) -> bytes:
    values = {
        "jpg": b"\xff\xd8\xff\xe0LeadFlow JPEG\xff\xd9",
        "jpeg": b"\xff\xd8\xff\xe0LeadFlow JPEG\xff\xd9",
        "png": b"\x89PNG\r\n\x1a\nLeadFlow PNG",
        "webp": b"RIFF\x0c\x00\x00\x00WEBPVP8 LeadFlow",
        "pdf": b"%PDF-1.4\nLeadFlow PDF\n%%EOF",
        "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"WordDocument" + b"\0" * 32,
        "xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"Workbook" + b"\0" * 32,
        "ppt": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"PowerPoint Document" + b"\0" * 32,
        "docx": office_zip("word"), "xlsx": office_zip("xl"), "pptx": office_zip("ppt"),
        "txt": "LeadFlow — текстовый файл".encode(), "csv": "company,email\nTest,test@example.test\n".encode(),
    }
    return values[extension]


def upload(name: str, content: bytes, mime: str | None = None) -> UploadFile:
    headers = Headers({"content-type": mime}) if mime else Headers()
    return UploadFile(filename=name, file=io.BytesIO(content), headers=headers)


@pytest.fixture()
def attachment_db(tmp_path: Path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        direction = Direction(name="Вложения", slug="attachments", sheet_tab="Вложения", active=False)
        session.add(direction); session.commit(); session.refresh(direction)
        yield session, direction, Settings(attachment_storage_dir=str(tmp_path), public_base_url="https://lead.test")


@pytest.mark.parametrize("extension", ["jpg", "jpeg", "png", "webp", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv"])
def test_allowed_attachment_upload_and_binary_integrity(attachment_db, extension: str) -> None:
    session, direction, settings = attachment_db
    content = fixture_bytes(extension)
    row = asyncio.run(save_attachment(session, settings, direction.id, upload(f"test.{extension}", content, MIMES[extension])))
    assert row.content_type == MIMES[extension]
    assert row.sha256 == hashlib.sha256(content).hexdigest()
    assert attachment_path(settings, row.storage_name).read_bytes() == content


@pytest.mark.parametrize("filename", ["program.exe", "document.pdf.exe", "document.exe.pdf", "script.js", "run.sh"])
def test_unsafe_extension_is_rejected(attachment_db, filename: str) -> None:
    session, direction, settings = attachment_db
    with pytest.raises(ValueError, match="Формат файла не поддерживается"):
        asyncio.run(save_attachment(session, settings, direction.id, upload(filename, b"MZ")))


@pytest.mark.parametrize("filename", ["../../document.pdf", "..\\..\\document.pdf", "folder/document.pdf"])
def test_path_traversal_filename_is_rejected(attachment_db, filename: str) -> None:
    session, direction, settings = attachment_db
    with pytest.raises(ValueError, match="Некорректное имя файла"):
        asyncio.run(save_attachment(session, settings, direction.id, upload(filename, fixture_bytes("pdf"), MIMES["pdf"])))


def test_oversize_attachment_is_rejected(attachment_db) -> None:
    session, direction, settings = attachment_db
    content = b"%PDF-1.4\n" + b"0" * MAX_FILE_BYTES + b"\n%%EOF"
    with pytest.raises(ValueError, match="Максимальный размер — 20 МБ"):
        asyncio.run(save_attachment(session, settings, direction.id, upload("large.pdf", content, MIMES["pdf"])))


def test_extension_mime_and_binary_signature_must_agree(attachment_db) -> None:
    session, direction, settings = attachment_db
    with pytest.raises(ValueError, match="не соответствует"):
        asyncio.run(save_attachment(session, settings, direction.id, upload("fake.pdf", fixture_bytes("jpg"), MIMES["pdf"])))
    with pytest.raises(ValueError, match="не соответствует"):
        asyncio.run(save_attachment(session, settings, direction.id, upload("fake.pdf", fixture_bytes("pdf"), MIMES["jpg"])))


def test_email_mime_attachments_preserve_filename_type_size_and_sha(attachment_db, monkeypatch) -> None:
    session, direction, settings = attachment_db
    originals = {}
    for extension in ("pdf", "docx", "xlsx", "pptx", "jpg"):
        content = fixture_bytes(extension); originals[f"test.{extension}"] = content
        asyncio.run(save_attachment(session, settings, direction.id, upload(f"test.{extension}", content, MIMES[extension])))
    delivery = EmailDelivery(
        subject="Вложения", recipient_email="recipient@example.test", html_body="<p>Письмо</p>",
        text_body="Письмо", message_id="<attachments@example.test>",
        attachments_snapshot=active_attachments(session, direction.id),
    )
    account = MailAccount(name="Почта", from_email="sender@example.test", from_name="Мария")
    monkeypatch.setattr("app.services.mailing.get_settings", lambda: settings)
    message = build_email_message(delivery, account)
    received = {part.get_filename(): part for part in message.iter_attachments()}
    assert set(originals) <= set(received)
    for filename, content in originals.items():
        part = received[filename]
        assert part.get_content_type() == MIMES[filename.rsplit(".", 1)[-1]]
        payload = part.get_payload(decode=True)
        assert len(payload) == len(content)
        assert hashlib.sha256(payload).hexdigest() == hashlib.sha256(content).hexdigest()
