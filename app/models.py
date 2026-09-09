from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), index=True)
    city: Mapped[str | None] = mapped_column(String(255), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    region: Mapped[str | None] = mapped_column(String(255), index=True)
    company_email: Mapped[str | None] = mapped_column(String(320), index=True)
    company_phone: Mapped[str | None] = mapped_column(String(255))
    branches_count: Mapped[int | None] = mapped_column(Integer)
    decision_maker_name: Mapped[str | None] = mapped_column(String(255))
    decision_maker_position: Mapped[str | None] = mapped_column(String(255))
    decision_maker_email: Mapped[str | None] = mapped_column(String(320))
    decision_maker_phone: Mapped[str | None] = mapped_column(String(255))
    communication_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    action: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    inn: Mapped[str | None] = mapped_column(String(16))
    contact_person: Mapped[str | None] = mapped_column(String(255))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_address: Mapped[str | None] = mapped_column(Text)
    normalized_phone: Mapped[str | None] = mapped_column(String(32))
    website_domain: Mapped[str | None] = mapped_column(String(255))
    name_address_fingerprint: Mapped[str | None] = mapped_column(String(64))
    manually_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("uq_company_source_external", "source", "source_external_id", unique=True),
        Index("uq_company_domain", "website_domain", unique=True),
        Index("uq_company_phone", "normalized_phone", unique=True),
        Index("uq_company_inn", "inn", unique=True),
        Index("uq_company_name_address", "name_address_fingerprint", unique=True),
    )


class Direction(Base):
    __tablename__ = "directions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    sheet_tab: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    limit_new: Mapped[int] = mapped_column(Integer, default=50)
    schedule: Mapped[str | None] = mapped_column(String(100))
    email_enrichment_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_enrichment_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DirectionSearchQuery(Base):
    __tablename__ = "direction_search_queries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), index=True)
    query: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("direction_id", "query", name="uq_direction_query"),)


class DirectionLocation(Base):
    __tablename__ = "direction_locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), index=True)
    city: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("direction_id", "city", "region", name="uq_direction_location"),)


class DirectionSource(Base):
    __tablename__ = "direction_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("direction_id", "source", name="uq_direction_source"),)


class CompanyDirection(Base):
    __tablename__ = "company_directions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("company_id", "direction_id", name="uq_company_direction"),)


class CompanySourceRecord(Base):
    __tablename__ = "company_source_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_external_id: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(Text)
    query: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(255))
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("source", "source_external_id", name="uq_company_source_record_external"),
    )


class CompanyFieldProvenance(Base):
    __tablename__ = "company_field_provenance"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    discovery_method: Mapped[str] = mapped_column(String(64), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SheetRowMapping(Base):
    __tablename__ = "sheet_row_mappings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), index=True)
    spreadsheet_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sheet_tab: Mapped[str] = mapped_column(String(100), nullable=False)
    sheet_row: Mapped[int] = mapped_column(Integer, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("company_id", "direction_id", "spreadsheet_id", name="uq_sheet_company_direction"),
        UniqueConstraint("spreadsheet_id", "sheet_tab", "sheet_row", name="uq_sheet_row"),
    )


class DirectionRun(Base):
    __tablename__ = "direction_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    limit_new: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceJob(Base):
    __tablename__ = "source_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(255), nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    limit_new: Mapped[int] = mapped_column(Integer, default=50)
    schedule: Mapped[str | None] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ParserRun(Base):
    __tablename__ = "parser_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    scanned: Mapped[int] = mapped_column(Integer, default=0)
    inserted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MailAccount(Base):
    __tablename__ = "mail_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    from_name: Mapped[str | None] = mapped_column(String(255))
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_login: Mapped[str] = mapped_column(String(320), nullable=False)
    smtp_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    smtp_security: Mapped[str] = mapped_column(String(16), default="starttls")
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_login: Mapped[str] = mapped_column(String(320), nullable=False)
    imap_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), index=True)
    subject_template: Mapped[str] = mapped_column(Text, nullable=False)
    html_template: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), index=True)
    city: Mapped[str | None] = mapped_column(String(255), index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id"), nullable=False)
    template_id: Mapped[str] = mapped_column(ForeignKey("email_templates.id"), nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(100))
    daily_limit: Mapped[int] = mapped_column(Integer, default=50)
    run_limit: Mapped[int] = mapped_column(Integer, default=20)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id"), nullable=False, index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("email_templates.id"), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    tracking_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("uq_delivery_company_recipient", "company_id", "recipient_email", unique=True),)


class TrackedLink(Base):
    __tablename__ = "tracked_links"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("email_deliveries.id"), nullable=False, index=True)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)


class Suppression(Base):
    __tablename__ = "suppressions"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboundReply(Base):
    __tablename__ = "inbound_replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    delivery_id: Mapped[str | None] = mapped_column(ForeignKey("email_deliveries.id"), index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id"), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    text_body: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GoogleSheetsConfig(Base):
    __tablename__ = "google_sheets_config"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    spreadsheet_id: Mapped[str] = mapped_column(String(255), nullable=False)
    worksheet_name: Mapped[str] = mapped_column(String(255), default="Companies")
    credentials_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AIConfig(Base):
    __tablename__ = "ai_config"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    api_base: Mapped[str] = mapped_column(String(500), default="https://api.openai.com/v1")
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PhraseSearchRun(Base):
    __tablename__ = "phrase_search_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
