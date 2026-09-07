from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CompanyRead(ORMModel):
    id: str
    source: str
    source_external_id: str | None
    source_url: str | None
    company_name: str
    category: str | None
    city: str | None
    address: str | None
    phone: str | None
    email: str | None
    website: str | None
    inn: str | None
    contact_person: str | None
    collected_at: datetime
    raw_data: dict[str, Any]
    manually_blocked: bool


class CompanyUpdate(BaseModel):
    email: str | None = None
    contact_person: str | None = None
    manually_blocked: bool | None = None


class SourceJobCreate(BaseModel):
    source: Literal["yandex_maps", "two_gis"]
    category: str = Field(min_length=1, max_length=255)
    city: str = Field(min_length=1, max_length=255)
    keywords: list[str] = Field(default_factory=list)
    limit_new: int = Field(default=50, ge=1, le=10_000)
    schedule: str | None = None
    active: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class SourceJobRead(SourceJobCreate, ORMModel):
    id: str
    created_at: datetime
    updated_at: datetime


class SourceJobUpdate(BaseModel):
    category: str | None = None
    city: str | None = None
    keywords: list[str] | None = None
    limit_new: int | None = Field(default=None, ge=1, le=10_000)
    schedule: str | None = None
    active: bool | None = None
    options: dict[str, Any] | None = None


class ParserRunRead(ORMModel):
    id: str
    job_id: str
    status: str
    scanned: int
    inserted: int
    duplicates: int
    errors: int
    message: str | None
    checkpoint: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None


class MailAccountCreate(BaseModel):
    name: str
    from_email: str
    from_name: str | None = None
    smtp_host: str
    smtp_port: int = 587
    smtp_login: str
    smtp_password: str
    smtp_security: Literal["none", "starttls", "ssl"] = "starttls"
    imap_host: str
    imap_port: int = 993
    imap_login: str
    imap_password: str
    active: bool = True
    daily_limit: int = Field(default=100, ge=1, le=10000)


class MailAccountRead(ORMModel):
    id: str
    name: str
    from_email: str
    from_name: str | None
    smtp_host: str
    smtp_port: int
    smtp_login: str
    smtp_security: str
    imap_host: str
    imap_port: int
    imap_login: str
    active: bool
    daily_limit: int
    created_at: datetime


class MailAccountUpdate(BaseModel):
    active: bool | None = None
    daily_limit: int | None = Field(default=None, ge=1, le=10000)
    smtp_password: str | None = None
    imap_password: str | None = None


class EmailTemplateCreate(BaseModel):
    name: str
    category: str | None = None
    subject_template: str
    html_template: str
    active: bool = True


class EmailTemplateRead(EmailTemplateCreate, ORMModel):
    id: str
    created_at: datetime


class EmailTemplateUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    subject_template: str | None = None
    html_template: str | None = None
    active: bool | None = None


class CampaignCreate(BaseModel):
    name: str
    category: str | None = None
    city: str | None = None
    mailbox_id: str
    template_id: str
    schedule: str | None = None
    daily_limit: int = Field(default=50, ge=1)
    run_limit: int = Field(default=20, ge=1)
    active: bool = False


class CampaignRead(CampaignCreate, ORMModel):
    id: str
    created_at: datetime


class CampaignUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    city: str | None = None
    mailbox_id: str | None = None
    template_id: str | None = None
    schedule: str | None = None
    daily_limit: int | None = Field(default=None, ge=1)
    run_limit: int | None = Field(default=None, ge=1)
    active: bool | None = None


class DeliveryRead(ORMModel):
    id: str
    campaign_id: str | None
    company_id: str
    mailbox_id: str
    template_id: str
    recipient_email: str
    subject: str
    status: str
    error: str | None
    sent_at: datetime | None
    opened_at: datetime | None
    clicked_at: datetime | None
    replied_at: datetime | None
    bounced_at: datetime | None
    unsubscribed_at: datetime | None


class GoogleSheetsConfigCreate(BaseModel):
    spreadsheet_id: str
    worksheet_name: str = "Companies"
    service_account_json: dict[str, Any]
    active: bool = True


class GoogleSheetsConfigRead(ORMModel):
    id: str
    spreadsheet_id: str
    worksheet_name: str
    active: bool
    updated_at: datetime


class AIConfigCreate(BaseModel):
    api_base: str = "https://api.openai.com/v1"
    api_key: str
    model: str
    prompt_template: str
    active: bool = True


class AIConfigRead(ORMModel):
    id: str
    api_base: str
    model: str
    prompt_template: str
    active: bool
    updated_at: datetime


class PhraseSearchCreate(BaseModel):
    phrase: str = Field(min_length=3)
    limit: int = Field(default=20, ge=1, le=100)


class PhraseSearchRead(ORMModel):
    id: str
    phrase: str
    status: str
    result_count: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class PersonalizedSendCreate(BaseModel):
    mailbox_id: str
    template_id: str
    ai_config_id: str
