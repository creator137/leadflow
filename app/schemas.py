from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    region: str | None
    company_email: str | None
    company_phone: str | None
    branches_count: int | None
    decision_maker_name: str | None
    decision_maker_position: str | None
    decision_maker_email: str | None
    decision_maker_phone: str | None
    communication_started_at: datetime | None
    action: str | None
    result: str | None
    website: str | None
    inn: str | None
    contact_person: str | None
    collected_at: datetime
    raw_data: dict[str, Any]
    manually_blocked: bool


class CompanyUpdate(BaseModel):
    email: str | None = None
    contact_person: str | None = None
    company_email: str | None = None
    company_phone: str | None = None
    decision_maker_name: str | None = None
    decision_maker_position: str | None = None
    decision_maker_email: str | None = None
    decision_maker_phone: str | None = None
    communication_started_at: datetime | None = None
    action: str | None = None
    result: str | None = None
    website: str | None = None
    manually_blocked: bool | None = None


class DirectionQueryInput(BaseModel):
    query: str = Field(min_length=1, max_length=255)
    active: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)


class DirectionLocationInput(BaseModel):
    city: str = Field(min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=255)
    active: bool = True


class DirectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    sheet_tab: str = Field(min_length=1, max_length=100)
    active: bool = True
    limit_new: int = Field(default=50, ge=1, le=10_000)
    schedule: str | None = None
    email_enrichment_enabled: bool = False
    ai_enrichment_enabled: bool = False
    queries: list[DirectionQueryInput] = Field(min_length=1)
    locations: list[DirectionLocationInput] = Field(min_length=1)
    sources: list[Literal["yandex_maps", "two_gis"]] = Field(min_length=1)

    @field_validator("schedule")
    @classmethod
    def valid_cron(cls, value: str | None) -> str | None:
        if value:
            CronTrigger.from_crontab(value, timezone="UTC")
        return value

    @field_validator("sheet_tab")
    @classmethod
    def valid_sheet_tab(cls, value: str) -> str:
        if any(character in value for character in "[]:*?/\\"):
            raise ValueError("Google Sheet tab contains a forbidden character")
        return value.strip()


class DirectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    sheet_tab: str | None = Field(default=None, min_length=1, max_length=100)
    active: bool | None = None
    limit_new: int | None = Field(default=None, ge=1, le=10_000)
    schedule: str | None = None
    email_enrichment_enabled: bool | None = None
    ai_enrichment_enabled: bool | None = None
    queries: list[DirectionQueryInput] | None = None
    locations: list[DirectionLocationInput] | None = None
    sources: list[Literal["yandex_maps", "two_gis"]] | None = None

    @field_validator("schedule")
    @classmethod
    def valid_cron(cls, value: str | None) -> str | None:
        if value:
            CronTrigger.from_crontab(value, timezone="UTC")
        return value

    @field_validator("sheet_tab")
    @classmethod
    def valid_sheet_tab(cls, value: str | None) -> str | None:
        if value and any(character in value for character in "[]:*?/\\"):
            raise ValueError("Google Sheet tab contains a forbidden character")
        return value.strip() if value else value


class DirectionWizardCreate(BaseModel):
    direction: DirectionCreate
    proposal_subject: str = Field(min_length=1, max_length=500)
    proposal_greeting: str = Field(min_length=1, max_length=2000)
    proposal_main_body: str = Field(min_length=1, max_length=10_000)
    proposal_extra_block: str = Field(default="", max_length=4000)
    proposal_cta: str = Field(min_length=1, max_length=4000)
    proposal_signature_override: str = Field(default="", max_length=4000)
    proposal_ai_instruction: str = Field(min_length=1, max_length=4000)
    proposal_ai_enabled: bool = True
    email_template_subject: str = Field(min_length=1, max_length=500)
    email_template_text: str = Field(min_length=1, max_length=20_000)
    mailbox_id: str
    campaign_name: str = Field(min_length=1, max_length=255)
    campaign_daily_limit: int = Field(default=50, ge=1, le=10_000)
    campaign_schedule: str | None = None
    campaign_enabled: bool = False

    @field_validator("campaign_schedule")
    @classmethod
    def valid_campaign_schedule(cls, value: str | None) -> str | None:
        if value:
            CronTrigger.from_crontab(value, timezone="UTC")
        return value


class DirectionQueryRead(DirectionQueryInput, ORMModel):
    id: str


class DirectionLocationRead(DirectionLocationInput, ORMModel):
    id: str


class DirectionRead(ORMModel):
    id: str
    name: str
    slug: str
    sheet_tab: str
    active: bool
    archived_at: datetime | None
    limit_new: int
    schedule: str | None
    email_enrichment_enabled: bool
    ai_enrichment_enabled: bool
    queries: list[DirectionQueryRead]
    locations: list[DirectionLocationRead]
    sources: list[str]
    created_at: datetime
    updated_at: datetime


class DirectionRunRead(ORMModel):
    id: str
    direction_id: str
    status: str
    limit_new: int
    scanned: int
    inserted: int
    duplicates: int
    errors: int
    message: str | None
    checkpoint: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None


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
    reply_to: str | None = None
    smtp_host: str
    smtp_port: int = 587
    smtp_login: str
    smtp_password: str
    smtp_security: Literal["none", "starttls", "ssl"] = "starttls"
    imap_host: str
    imap_port: int = 993
    imap_login: str
    imap_password: str
    imap_security: Literal["none", "starttls", "ssl"] = "ssl"
    forward_replies_to: str | None = None
    active: bool = True
    is_primary: bool = False
    daily_limit: int = Field(default=100, ge=1, le=10000)

    @field_validator("from_email", "reply_to", "forward_replies_to")
    @classmethod
    def valid_mail_address(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Укажите корректный email")
        return value


class MailAccountRead(ORMModel):
    id: str
    name: str
    from_email: str
    from_name: str | None
    reply_to: str | None
    smtp_host: str
    smtp_port: int
    smtp_login: str
    smtp_security: str
    imap_host: str
    imap_port: int
    imap_login: str
    imap_security: str
    forward_replies_to: str | None
    active: bool
    is_primary: bool
    daily_limit: int
    created_at: datetime
    updated_at: datetime


class MailAccountUpdate(BaseModel):
    name: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    reply_to: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_login: str | None = None
    smtp_security: Literal["none", "starttls", "ssl"] | None = None
    imap_host: str | None = None
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_login: str | None = None
    imap_security: Literal["none", "starttls", "ssl"] | None = None
    forward_replies_to: str | None = None
    active: bool | None = None
    is_primary: bool | None = None
    daily_limit: int | None = Field(default=None, ge=1, le=10000)
    smtp_password: str | None = None
    imap_password: str | None = None

    @field_validator("from_email", "reply_to", "forward_replies_to")
    @classmethod
    def valid_mail_address(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Укажите корректный email")
        return value


class EmailTemplateCreate(BaseModel):
    name: str
    category: str | None = None
    direction_id: str | None = None
    subject_template: str
    html_template: str
    text_template: str = ""
    active: bool = True


class EmailTemplateRead(EmailTemplateCreate, ORMModel):
    id: str
    created_at: datetime
    updated_at: datetime


class EmailTemplateUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    direction_id: str | None = None
    subject_template: str | None = None
    html_template: str | None = None
    text_template: str | None = None
    active: bool | None = None


class CampaignCreate(BaseModel):
    name: str
    category: str | None = None
    city: str | None = None
    direction_id: str | None = None
    mailbox_id: str
    template_id: str
    schedule: str | None = None
    daily_limit: int = Field(default=50, ge=1)
    run_limit: int = Field(default=20, ge=1)
    sending_interval_seconds: int = Field(default=60, ge=1, le=86400)
    cooldown_days: int = Field(default=30, ge=0, le=3650)
    status: Literal["paused", "running", "completed"] = "paused"
    active: bool = False

    @field_validator("schedule")
    @classmethod
    def valid_schedule(cls, value: str | None) -> str | None:
        if value:
            CronTrigger.from_crontab(value, timezone="UTC")
        return value


class CampaignRead(CampaignCreate, ORMModel):
    id: str
    created_at: datetime
    updated_at: datetime
    last_run_at: datetime | None = None
    last_queued: int = 0
    last_error: str | None = None


class CampaignUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    city: str | None = None
    direction_id: str | None = None
    mailbox_id: str | None = None
    template_id: str | None = None
    schedule: str | None = None
    daily_limit: int | None = Field(default=None, ge=1)
    run_limit: int | None = Field(default=None, ge=1)
    sending_interval_seconds: int | None = Field(default=None, ge=1, le=86400)
    cooldown_days: int | None = Field(default=None, ge=0, le=3650)
    status: Literal["paused", "running", "completed"] | None = None
    active: bool | None = None

    @field_validator("schedule")
    @classmethod
    def valid_schedule(cls, value: str | None) -> str | None:
        if value:
            CronTrigger.from_crontab(value, timezone="UTC")
        return value


class DeliveryRead(ORMModel):
    id: str
    campaign_id: str | None
    direction_id: str | None
    company_id: str
    mailbox_id: str
    template_id: str
    recipient_email: str
    recipient_name: str | None
    subject: str
    status: str
    message_id: str | None
    provider_message_id: str | None
    in_reply_to: str | None
    error: str | None
    sent_at: datetime | None
    opened_at: datetime | None
    clicked_at: datetime | None
    replied_at: datetime | None
    bounced_at: datetime | None
    unsubscribed_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime | None
    created_at: datetime


class TemplatePreview(BaseModel):
    company_id: str


class TemplateTestSend(BaseModel):
    mailbox_id: str
    recipient_email: str
    company_id: str


class ManualSendCreate(BaseModel):
    mailbox_id: str
    template_id: str
    direction_id: str | None = None
    subject: str | None = None
    html_body: str | None = None
    text_body: str | None = None


class SuppressionCreate(BaseModel):
    email: str
    reason: Literal["unsubscribe", "hard_bounce", "manual", "complaint", "invalid"] = "manual"
    note: str | None = None


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
    direction_id: str
    city: str = Field(min_length=2, max_length=255)
    region: str | None = Field(default=None, max_length=255)
    phrases: list[str] = Field(min_length=1, max_length=20)
    limit: int = Field(default=20, ge=1, le=100)
    use_ai: bool = True

    @field_validator("phrases")
    @classmethod
    def clean_phrases(cls, values: list[str]) -> list[str]:
        result = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not result or any(len(value) < 3 or len(value) > 255 for value in result):
            raise ValueError("Каждая фраза должна содержать от 3 до 255 символов.")
        return result


class PhraseSearchRead(ORMModel):
    id: str
    direction_id: str | None
    phrase: str
    city: str | None
    region: str | None
    use_ai: bool
    status: str
    urls_discovered: int
    result_count: int
    new_count: int
    duplicate_count: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class SheetActionRequest(BaseModel):
    company_id: str = Field(min_length=1, max_length=36)


class PersonalizedSendCreate(BaseModel):
    mailbox_id: str
    template_id: str
    request_key: str
    subject: str | None = None
    html_body: str | None = None
    text_body: str | None = None


class PersonalizationPreviewCreate(BaseModel):
    template_id: str
    mailbox_id: str | None = None
    regenerate: bool = False


class ProposalDraftCreate(BaseModel):
    mailbox_id: str | None = None
    regenerate: bool = False


class ProposalDraftUpdate(BaseModel):
    subject: str | None = Field(default=None, max_length=500)
    greeting: str | None = Field(default=None, max_length=2000)
    main_body: str | None = Field(default=None, max_length=10_000)
    ai_personalization: str | None = Field(default=None, max_length=4000)
    extra_block: str | None = Field(default=None, max_length=4000)
    cta: str | None = Field(default=None, max_length=4000)
    signature: str | None = Field(default=None, max_length=4000)
    mailbox_id: str | None = None
    recipient_email: str | None = Field(default=None, max_length=320)

    @field_validator("recipient_email")
    @classmethod
    def valid_recipient_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Укажите корректный email получателя")
        return value


class ProposalTemplateUpdate(BaseModel):
    subject: str | None = Field(default=None, max_length=500)
    greeting: str | None = Field(default=None, max_length=2000)
    main_body: str | None = Field(default=None, max_length=10_000)
    extra_block: str | None = Field(default=None, max_length=4000)
    cta: str | None = Field(default=None, max_length=4000)
    signature: str | None = Field(default=None, max_length=4000)
    ai_instruction: str | None = Field(default=None, max_length=4000)
    ai_personalization_enabled: bool | None = None
    active: bool | None = None


class ProposalTestSend(BaseModel):
    recipient_email: str


class AISettingsUpdate(BaseModel):
    enrichment_enabled: bool | None = None
    personalization_enabled: bool | None = None
    daily_request_limit: int | None = Field(default=None, ge=1, le=10_000)


class SenderSettingsUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    position: str | None = Field(default=None, max_length=255)
    company_name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=320)
    website: str | None = Field(default=None, max_length=2000)
    product_description: str = Field(default="", max_length=10_000)
    signature_text: str = Field(default="", max_length=4000)
