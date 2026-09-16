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
    automatic_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_templates.id", ondelete="SET NULL", use_alter=True), index=True
    )
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


class DirectionAttachment(Base):
    __tablename__ = "direction_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(150), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("direction_id", "sha256", name="uq_direction_attachment_sha"),)


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


class SearchObservation(Base):
    """One factual parser result, retained even when it is a duplicate."""
    __tablename__ = "search_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("direction_runs.id", ondelete="CASCADE"), index=True)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    query: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    city: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_external_id: Mapped[str | None] = mapped_column(String(255))
    source_url: Mapped[str | None] = mapped_column(Text)
    is_new: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    matched_by: Mapped[str | None] = mapped_column(String(64))
    has_phone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_website: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_branches_count: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_type: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


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
    last_synced_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("company_id", "direction_id", "spreadsheet_id", name="uq_sheet_company_direction"),
        UniqueConstraint("spreadsheet_id", "sheet_tab", "sheet_row", name="uq_sheet_row"),
    )


class GoogleSyncRun(Base):
    __tablename__ = "google_sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    config_id: Mapped[str] = mapped_column(ForeignKey("google_sheets_config.id", ondelete="CASCADE"), index=True)
    direction_id: Mapped[str | None] = mapped_column(ForeignKey("directions.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    manual_changes: Mapped[int] = mapped_column(Integer, default=0)
    conflicts: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    reply_to: Mapped[str | None] = mapped_column(String(320))
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_login: Mapped[str] = mapped_column(String(320), nullable=False)
    smtp_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    smtp_security: Mapped[str] = mapped_column(String(16), default="starttls")
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_login: Mapped[str] = mapped_column(String(320), nullable=False)
    imap_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    imap_security: Mapped[str] = mapped_column(String(16), default="ssl")
    imap_uidvalidity: Mapped[int | None] = mapped_column(Integer)
    imap_last_uid: Mapped[int] = mapped_column(Integer, default=0)
    forward_replies_to: Mapped[str | None] = mapped_column(String(320))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    daily_limit: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EmailTemplate(Base):
    __tablename__ = "email_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), index=True)
    direction_id: Mapped[str | None] = mapped_column(ForeignKey("directions.id", ondelete="SET NULL"), index=True)
    subject_template: Mapped[str] = mapped_column(Text, nullable=False)
    html_template: Mapped[str] = mapped_column(Text, nullable=False)
    text_template: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DirectionProposalTemplate(Base):
    __tablename__ = "direction_proposal_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction_id: Mapped[str] = mapped_column(
        ForeignKey("directions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    greeting: Mapped[str] = mapped_column(Text, nullable=False)
    main_body: Mapped[str] = mapped_column(Text, nullable=False)
    extra_block: Mapped[str] = mapped_column(Text, default="")
    cta: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    ai_instruction: Mapped[str] = mapped_column(Text, nullable=False)
    ai_personalization_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("direction_id", name="uq_direction_proposal_template_direction"),)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), index=True)
    city: Mapped[str | None] = mapped_column(String(255), index=True)
    direction_id: Mapped[str | None] = mapped_column(ForeignKey("directions.id", ondelete="SET NULL"), index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id"), nullable=False)
    template_id: Mapped[str] = mapped_column(ForeignKey("email_templates.id"), nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(100))
    daily_limit: Mapped[int] = mapped_column(Integer, default=50)
    run_limit: Mapped[int] = mapped_column(Integer, default=20)
    sending_interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    cooldown_days: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(32), default="paused", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_queued: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EmailDelivery(Base):
    __tablename__ = "email_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    direction_id: Mapped[str | None] = mapped_column(ForeignKey("directions.id", ondelete="SET NULL"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id"), nullable=False, index=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("email_templates.id"), nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    recipient_name: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, default="")
    send_mode: Mapped[str] = mapped_column(String(32), default="campaign")
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    tracking_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    unsubscribe_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    unsubscribe_url: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(998))
    message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attachments_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("uq_delivery_campaign_company_recipient", "campaign_id", "company_id", "recipient_email", unique=True),
    )


class EmailEvent(Base):
    __tablename__ = "email_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("email_deliveries.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TrackedLink(Base):
    __tablename__ = "tracked_links"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("email_deliveries.id"), nullable=False, index=True)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)


class Suppression(Base):
    __tablename__ = "suppressions"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    source_delivery_id: Mapped[str | None] = mapped_column(ForeignKey("email_deliveries.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InboundReply(Base):
    __tablename__ = "inbound_replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    delivery_id: Mapped[str | None] = mapped_column(ForeignKey("email_deliveries.id"), index=True)
    mailbox_id: Mapped[str] = mapped_column(ForeignKey("mail_accounts.id"), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(998), unique=True)
    imap_uid: Mapped[int | None] = mapped_column(Integer)
    imap_uidvalidity: Mapped[int | None] = mapped_column(Integer)
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[str | None] = mapped_column(Text)
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    text_body: Mapped[str | None] = mapped_column(Text)
    bounce_type: Mapped[str | None] = mapped_column(String(32))
    forwarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("mailbox_id", "imap_uidvalidity", "imap_uid", name="uq_inbound_mailbox_uid"),
    )


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


class AISettings(Base):
    __tablename__ = "ai_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    enrichment_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    personalization_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_request_limit: Mapped[int] = mapped_column(Integer, default=25)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SenderSettings(Base):
    __tablename__ = "sender_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[str | None] = mapped_column(String(255))
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320))
    website: Mapped[str | None] = mapped_column(Text)
    product_description: Mapped[str] = mapped_column(Text, default="")
    logo_path: Mapped[str] = mapped_column(String(255), default="logo.jpg")
    signature_text: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebsiteAnalysis(Base):
    __tablename__ = "website_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    website: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_chars: Mapped[int] = mapped_column(Integer, default=0)
    cleaned_chars: Mapped[int] = mapped_column(Integer, default=0)
    relevant_chars: Mapped[int] = mapped_column(Integer, default=0)
    page_blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    deterministic_fields: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ai_enrichment_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AIRequestLog(Base):
    __tablename__ = "ai_request_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    request_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    response_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class SheetPersonalizationDraft(Base):
    __tablename__ = "sheet_personalization_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    direction_id: Mapped[str] = mapped_column(ForeignKey("directions.id", ondelete="CASCADE"), nullable=False, index=True)
    config_id: Mapped[str | None] = mapped_column(ForeignKey("google_sheets_config.id", ondelete="CASCADE"))
    template_id: Mapped[str] = mapped_column(ForeignKey("email_templates.id"), nullable=False)
    proposal_template_id: Mapped[str | None] = mapped_column(
        ForeignKey("direction_proposal_templates.id", ondelete="SET NULL"), index=True
    )
    template_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mailbox_id: Mapped[str | None] = mapped_column(ForeignKey("mail_accounts.id"))
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    command_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    request_key: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="preparing", index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    greeting: Mapped[str | None] = mapped_column(Text)
    main_body: Mapped[str | None] = mapped_column(Text)
    ai_personalization: Mapped[str | None] = mapped_column(Text)
    extra_block: Mapped[str | None] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(Text)
    signature: Mapped[str | None] = mapped_column(Text)
    ai_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    ai_response_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    html_body: Mapped[str | None] = mapped_column(Text)
    text_body: Mapped[str | None] = mapped_column(Text)
    html_snapshot: Mapped[str | None] = mapped_column(Text)
    sent_html_snapshot: Mapped[str | None] = mapped_column(Text)
    sent_text_snapshot: Mapped[str | None] = mapped_column(Text)
    attachments_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    delivery_id: Mapped[str | None] = mapped_column(ForeignKey("email_deliveries.id", ondelete="SET NULL"), unique=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PhraseSearchRun(Base):
    __tablename__ = "phrase_search_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction_id: Mapped[str | None] = mapped_column(ForeignKey("directions.id", ondelete="SET NULL"), index=True)
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(String(255), index=True)
    region: Mapped[str | None] = mapped_column(String(255))
    use_ai: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    urls_discovered: Mapped[int] = mapped_column(Integer, default=0)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PhraseSearchResult(Base):
    __tablename__ = "phrase_search_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("phrase_search_runs.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    phrase: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    company_name: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(255))
    extraction_method: Mapped[str] = mapped_column(String(32), default="deterministic")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (UniqueConstraint("run_id", "source_url", name="uq_phrase_result_run_url"),)
