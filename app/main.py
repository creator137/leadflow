from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings, validate_production_secrets
from app.db import SessionLocal, get_db
from app.models import (
    AIConfig, Campaign, Company, CompanyDirection, CompanyFieldProvenance, CompanySourceRecord,
    Direction, DirectionAttachment, DirectionProposalTemplate, DirectionRun, EmailDelivery, EmailTemplate, GoogleSheetsConfig, GoogleSyncRun,
    MailAccount, ParserRun, PhraseSearchResult, PhraseSearchRun, SheetPersonalizationDraft, SheetRowMapping, SourceJob, Suppression, TrackedLink,
)
from app.schemas import (
    AIConfigCreate, AIConfigRead, CampaignCreate, CampaignRead, CampaignUpdate, CompanyRead, CompanyUpdate,
    DeliveryRead, DirectionCreate, DirectionRead, DirectionRunRead, DirectionUpdate, DirectionWizardCreate, EmailTemplateCreate,
    EmailTemplateRead, EmailTemplateUpdate, GoogleSheetsConfigCreate, GoogleSheetsConfigRead, MailAccountCreate,
    MailAccountRead, MailAccountUpdate, ManualSendCreate, ParserRunRead, SuppressionCreate, TemplatePreview, TemplateTestSend,
    AISettingsUpdate, PersonalizationPreviewCreate, PersonalizedSendCreate, PhraseSearchCreate, PhraseSearchRead,
    ProposalDraftCreate, ProposalDraftUpdate, ProposalTemplateUpdate, ProposalTestSend, SenderSettingsUpdate,
    SheetActionRequest, SourceJobCreate, SourceJobRead, SourceJobUpdate,
)
from app.services.collection import execute_job
from app.services.analytics import analytics as build_analytics
from app.services.ai_usage import ai_usage_details, ai_usage_journal
from app.services.company_enrichment import CompanyEnrichmentService
from app.services.direction_pipeline import execute_direction_pipeline
from app.services.data_quality import backfill_structured_business_fields
from app.services.directions import archive_direction, create_direction, serialize_direction, update_direction
from app.services.google_sheets import GoogleSheetsSyncService, active_sheets_config, sync_companies, worksheet_from_config
from app.services.mailing import build_delivery, diagnose_imap, diagnose_smtp, execute_campaign, process_queue, render_template_parts, sender_template_context, send_delivery
from app.services.deepseek import DeepSeekClient, DeepSeekError, ai_settings as get_ai_settings
from app.services.personalized import PersonalizationFragments, prepare_personalization, send_personalized
from app.services.sheet_personalization import send_sheet_draft, serialize_draft
from app.services.sheet_actions import execute_sheet_action
from app.services.proposals import (
    ensure_all_proposal_templates, ensure_direction_email_template, ensure_proposal_template, finalize_proposal_delivery, get_sender_settings, prepare_proposal_draft, render_proposal,
    send_proposal_draft, serialize_proposal_template, update_proposal_draft,
)
from app.services.admin_self_service import (
    create_direction_bundle, delivery_timeline, email_journal, serialize_campaign, serialize_sender,
    update_sender_settings,
)
from app.services.attachments import attachment_dict, attachment_path, save_attachment
from app.services.sheet_personalization import _default_mailbox
from app.services.secrets import decrypt_secret, encrypt_secret
from app.services.provenance import apply_field
from app.services.user_errors import human_error
from app.services.email_events import record_email_event, sync_email_event_to_sheets
from app.sources.phrase_search import execute_phrase_search

app = FastAPI(title="LeadFlow", version="0.1.0")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def security_startup_check() -> None:
    validate_production_secrets(get_settings())
    with SessionLocal() as session:
        backfill_structured_business_fields(session)
        ensure_all_proposal_templates(session)


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    settings = get_settings()
    sheet_action = request.url.path.startswith("/api/google-sheets/actions/")
    if sheet_action:
        supplied = request.headers.get("X-LeadFlow-Sheet-Token", "")
        configured = settings.google_sheets_action_token or ""
        if not configured or not supplied or not secrets.compare_digest(supplied, configured):
            return Response("Unauthorized", status_code=401)
        return await call_next(request)
    protected = request.url.path == "/" or request.url.path.startswith(("/api", "/docs", "/redoc", "/openapi", "/proposal-drafts"))
    if protected and settings.admin_username and settings.admin_password:
        authorization = request.headers.get("Authorization", "")
        valid = False
        if authorization.startswith("Basic "):
            try:
                username, password = base64.b64decode(authorization[6:]).decode().split(":", 1)
                valid = secrets.compare_digest(username, settings.admin_username) and secrets.compare_digest(
                    password, settings.admin_password
                )
            except (ValueError, UnicodeDecodeError):
                pass
        if not valid:
            return Response(status_code=401, headers={"WWW-Authenticate": "Basic"})
    if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("X-LeadFlow-CSRF") != "1":
            return Response("CSRF validation failed", status_code=403)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def admin_panel() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/email-assets/bogorodsky-pryanik-logo.jpg", include_in_schema=False)
def proposal_logo() -> FileResponse:
    return FileResponse(Path(__file__).parent.parent / "logo.jpg", media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/system-settings")
def system_settings() -> dict[str, str | bool | int]:
    settings = get_settings()
    return {
        "public_base_url": settings.public_base_url,
        "chrome_binary": settings.chrome_binary or "auto",
        "source_max_scan": settings.source_max_scan,
        "yandex_grid": settings.yandex_grid,
        "yandex_enrich_emails": settings.yandex_enrich_emails,
        "search_provider": settings.search_provider,
        "search_provider_configured": bool(settings.serper_api_key),
        "manager_email_configured": bool(settings.manager_email),
        "admin_auth_enabled": bool(settings.admin_username and settings.admin_password),
        "google_sheets_spreadsheet_id": settings.google_sheets_spreadsheet_id,
        "google_service_account_configured": bool(settings.google_service_account_json),
    }


@app.get("/api/companies", response_model=list[CompanyRead])
def companies(
    city: str | None = None,
    category: str | None = None,
    source: str | None = None,
    direction: str | None = None,
    has_website: bool | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> list[Company]:
    query = select(Company).order_by(Company.collected_at.desc()).limit(limit).offset(offset)
    if direction:
        query = query.join(CompanyDirection).where(CompanyDirection.direction_id == direction)
    if city:
        query = query.where(Company.city == city)
    if category:
        query = query.where(Company.category == category)
    if source:
        query = query.where(Company.source == source)
    if has_website is True:
        query = query.where(Company.website.is_not(None))
    elif has_website is False:
        query = query.where(Company.website.is_(None))
    return list(session.scalars(query))


@app.get("/api/companies/{company_id}/details")
def company_details(company_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    company = session.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Компания не найдена")
    draft = session.scalar(select(SheetPersonalizationDraft).where(
        SheetPersonalizationDraft.company_id == company_id,
    ).order_by(SheetPersonalizationDraft.created_at.desc()).limit(1))
    return {
        "company": CompanyRead.model_validate(company).model_dump(mode="json"),
        "sources": [{"source": x.source, "source_url": x.source_url, "collected_at": x.collected_at}
                    for x in session.scalars(select(CompanySourceRecord).where(CompanySourceRecord.company_id == company_id).order_by(CompanySourceRecord.collected_at.desc()))],
        "changes": [{"field": x.field, "method": x.discovery_method, "source_url": x.source_url, "at": x.discovered_at}
                    for x in session.scalars(select(CompanyFieldProvenance).where(CompanyFieldProvenance.company_id == company_id).order_by(CompanyFieldProvenance.discovered_at.desc()))],
        "emails": [DeliveryRead.model_validate(x).model_dump(mode="json") for x in session.scalars(select(EmailDelivery).where(EmailDelivery.company_id == company_id).order_by(EmailDelivery.created_at.desc()))],
        "personalization_draft": serialize_draft(draft) if draft else None,
    }


@app.get("/api/analytics")
def analytics_api(
    date_from: datetime | None = None, date_to: datetime | None = None,
    direction: str | None = None, city: str | None = None, source: str | None = None,
    mailbox: str | None = None, template: str | None = None,
    session: Session = Depends(get_db),
) -> dict[str, object]:
    return build_analytics(session, date_from=date_from, date_to=date_to, direction=direction, city=city,
                           source=source, mailbox=mailbox, template=template)


@app.get("/api/analytics/ai-usage")
def ai_usage_api(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    company_id: str | None = None,
    operation: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    return ai_usage_journal(
        session,
        date_from=date_from,
        date_to=date_to,
        company_id=company_id,
        operation=operation,
        limit=limit,
        offset=offset,
    )


@app.get("/api/analytics/ai-usage/{log_id}")
def ai_usage_details_api(log_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    result = ai_usage_details(session, log_id)
    if result is None:
        raise HTTPException(404, "Запись расхода ИИ не найдена.")
    return result


@app.patch("/api/companies/{company_id}", response_model=CompanyRead)
def update_company(company_id: str, payload: CompanyUpdate, session: Session = Depends(get_db)) -> Company:
    company = session.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Компания не найдена.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        canonical = {"email": "company_email", "contact_person": "decision_maker_name"}.get(key, key)
        if canonical == "manually_blocked":
            company.manually_blocked = value
        else:
            apply_field(session, company, canonical, value, discovery_method="manual", confidence=1.0)
    session.commit()
    session.refresh(company)
    return company


@app.get("/api/directions", response_model=list[DirectionRead])
def directions(include_archived: bool = False, session: Session = Depends(get_db)) -> list[DirectionRead]:
    query = select(Direction).order_by(Direction.created_at.desc())
    if not include_archived:
        query = query.where(Direction.archived_at.is_(None))
    return [serialize_direction(session, item) for item in session.scalars(query)]


@app.post("/api/directions", response_model=DirectionRead, status_code=201)
def add_direction(payload: DirectionCreate, session: Session = Depends(get_db)) -> DirectionRead:
    try:
        direction = create_direction(session, payload)
        ensure_proposal_template(session, direction)
        ensure_direction_email_template(session, direction)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Direction name or Google Sheet tab already exists") from exc
    return serialize_direction(session, direction)


@app.post("/api/directions/wizard", status_code=201)
def add_direction_wizard(payload: DirectionWizardCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return create_direction_bundle(session, payload)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Направление или вкладка Google Таблицы с таким названием уже существует.") from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.patch("/api/directions/{direction_id}", response_model=DirectionRead)
def edit_direction(direction_id: str, payload: DirectionUpdate, session: Session = Depends(get_db)) -> DirectionRead:
    direction = session.get(Direction, direction_id)
    if not direction or direction.archived_at:
        raise HTTPException(404, "Direction not found")
    try:
        update_direction(session, direction, payload)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, "Direction name or Google Sheet tab already exists") from exc
    return serialize_direction(session, direction)


@app.delete("/api/directions/{direction_id}", status_code=204)
def remove_direction(direction_id: str, session: Session = Depends(get_db)) -> Response:
    direction = session.get(Direction, direction_id)
    if not direction:
        raise HTTPException(404, "Direction not found")
    archive_direction(session, direction)
    return Response(status_code=204)


@app.get("/api/directions/{direction_id}/attachments")
def direction_attachments(direction_id: str, session: Session = Depends(get_db)) -> list[dict[str, object]]:
    if not session.get(Direction, direction_id):
        raise HTTPException(404, "Направление не найдено.")
    return [attachment_dict(row) for row in session.scalars(select(DirectionAttachment).where(
        DirectionAttachment.direction_id == direction_id, DirectionAttachment.active.is_(True),
    ).order_by(DirectionAttachment.created_at))]


@app.post("/api/directions/{direction_id}/attachments", status_code=201)
async def upload_direction_attachment(
    direction_id: str, file: UploadFile = File(...), session: Session = Depends(get_db),
) -> dict[str, object]:
    if not session.get(Direction, direction_id):
        raise HTTPException(404, "Направление не найдено.")
    try:
        return attachment_dict(await save_attachment(session, get_settings(), direction_id, file))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/direction-attachments/{attachment_id}/download")
def download_direction_attachment(attachment_id: str, session: Session = Depends(get_db)) -> FileResponse:
    row = session.get(DirectionAttachment, attachment_id)
    if not row:
        raise HTTPException(404, "Файл не найден.")
    path = attachment_path(get_settings(), row.storage_name)
    if not path.is_file():
        raise HTTPException(404, "Файл не найден в хранилище.")
    return FileResponse(
        path, media_type=row.content_type, filename=row.filename,
        content_disposition_type="attachment", headers={"X-Content-Type-Options": "nosniff"},
    )


@app.delete("/api/direction-attachments/{attachment_id}", status_code=204)
def archive_direction_attachment(attachment_id: str, session: Session = Depends(get_db)) -> Response:
    row = session.get(DirectionAttachment, attachment_id)
    if not row:
        raise HTTPException(404, "Файл не найден.")
    row.active = False; session.commit()
    return Response(status_code=204)


def _active_sheets_config(session: Session) -> GoogleSheetsConfig | None:
    return active_sheets_config(session, get_settings())


def _run_direction_background(direction_id: str, run_id: str) -> None:
    with SessionLocal() as session:
        direction = session.get(Direction, direction_id)
        run = session.get(DirectionRun, run_id)
        if not direction or not run:
            return
        execute_direction_pipeline(session, direction, get_settings(), run)


@app.post("/api/directions/{direction_id}/run", response_model=DirectionRunRead, status_code=202)
def run_direction(direction_id: str, tasks: BackgroundTasks, session: Session = Depends(get_db)) -> DirectionRun:
    direction = session.get(Direction, direction_id)
    if not direction or direction.archived_at or not direction.active:
        raise HTTPException(404, "Active direction not found")
    run = DirectionRun(direction_id=direction.id, limit_new=direction.limit_new, status="queued")
    session.add(run)
    session.commit()
    session.refresh(run)
    tasks.add_task(_run_direction_background, direction.id, run.id)
    return run


@app.get("/api/direction-runs", response_model=list[DirectionRunRead])
def direction_runs(limit: int = Query(100, ge=1, le=1000), session: Session = Depends(get_db)) -> list[DirectionRun]:
    return list(session.scalars(select(DirectionRun).order_by(DirectionRun.started_at.desc()).limit(limit)))


@app.post("/api/direction-runs/{run_id}/resume", response_model=DirectionRunRead, status_code=202)
def resume_direction(run_id: str, tasks: BackgroundTasks, session: Session = Depends(get_db)) -> DirectionRun:
    run = session.get(DirectionRun, run_id)
    if not run:
        raise HTTPException(404, "Direction run not found")
    if run.status not in {"failed", "blocked", "exhausted"}:
        raise HTTPException(409, f"Direction run with status {run.status!r} cannot be resumed")
    direction = session.get(Direction, run.direction_id)
    if not direction:
        raise HTTPException(404, "Direction not found")
    run.status = "queued"
    run.message = None
    run.finished_at = None
    session.commit()
    tasks.add_task(_run_direction_background, direction.id, run.id)
    return run


@app.post("/api/companies/{company_id}/enrich")
def enrich_company(company_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    company = session.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    result = CompanyEnrichmentService(session, get_settings()).enrich(company, use_ai=True)
    config = _active_sheets_config(session)
    synced: dict[str, int] | None = None
    if config and result.get("deterministic", 0) + result.get("ai", 0) > 0:
        direction = session.scalar(select(Direction).join(CompanyDirection).where(CompanyDirection.company_id == company.id).limit(1))
        if direction:
            try:
                synced = GoogleSheetsSyncService(session, config).sync_direction(direction)
            except Exception as exc:
                session.rollback()
                result["sheets_message"] = human_error(exc)
    result["sheets"] = synced
    return result


@app.get("/api/dashboard")
def dashboard(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    category: str | None = None,
    city: str | None = None,
    source: str | None = None,
    mailbox: str | None = None,
    template: str | None = None,
    direction: str | None = None,
    session: Session = Depends(get_db),
) -> dict[str, int]:
    company_filters = []
    if date_from:
        company_filters.append(Company.collected_at >= date_from)
    if date_to:
        company_filters.append(Company.collected_at < date_to)
    if category:
        company_filters.append(Company.category == category)
    if city:
        company_filters.append(Company.city == city)
    if source:
        company_filters.append(Company.source == source)
    company_count = select(func.count()).select_from(Company).where(*company_filters)
    total = session.scalar(company_count) or 0
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    new_today = session.scalar(company_count.where(Company.collected_at >= today)) or 0
    with_email = session.scalar(company_count.where(Company.email.is_not(None))) or 0

    delivery_base = select(func.count()).select_from(EmailDelivery).join(Company).where(*company_filters)
    if mailbox:
        delivery_base = delivery_base.where(EmailDelivery.mailbox_id == mailbox)
    if template:
        delivery_base = delivery_base.where(EmailDelivery.template_id == template)
    if direction:
        delivery_base = delivery_base.where(EmailDelivery.direction_id == direction)
    queued = session.scalar(delivery_base.where(EmailDelivery.status.in_(["queued", "sending"]))) or 0
    sent = session.scalar(delivery_base.where(EmailDelivery.sent_at.is_not(None))) or 0
    send_errors = session.scalar(delivery_base.where(EmailDelivery.status == "send_error")) or 0
    opened = session.scalar(delivery_base.where(EmailDelivery.opened_at.is_not(None))) or 0
    clicked = session.scalar(delivery_base.where(EmailDelivery.clicked_at.is_not(None))) or 0
    replied = session.scalar(delivery_base.where(EmailDelivery.replied_at.is_not(None))) or 0
    bounce = session.scalar(delivery_base.where(EmailDelivery.bounced_at.is_not(None))) or 0
    unsubscribed = session.scalar(delivery_base.where(EmailDelivery.unsubscribed_at.is_not(None))) or 0
    return {
        "companies": total,
        "new_today": new_today,
        "email_found": with_email,
        "queued": queued,
        "sent": sent,
        "send_errors": send_errors,
        "bounce": bounce,
        "opened": opened,
        "clicked": clicked,
        "replied": replied,
        "unsubscribed": unsubscribed,
    }


@app.get("/api/parser-jobs", response_model=list[SourceJobRead])
def parser_jobs(session: Session = Depends(get_db)) -> list[SourceJob]:
    return list(session.scalars(select(SourceJob).order_by(SourceJob.created_at.desc())))


@app.post("/api/parser-jobs", response_model=SourceJobRead, status_code=status.HTTP_201_CREATED)
def create_parser_job(payload: SourceJobCreate, session: Session = Depends(get_db)) -> SourceJob:
    job = SourceJob(**payload.model_dump())
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@app.patch("/api/parser-jobs/{job_id}", response_model=SourceJobRead)
def update_parser_job(job_id: str, payload: SourceJobUpdate, session: Session = Depends(get_db)) -> SourceJob:
    job = session.get(SourceJob, job_id)
    if not job:
        raise HTTPException(404, "Parser job not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(job, key, value)
    session.commit()
    session.refresh(job)
    return job


def _run_in_background(job_id: str, run_id: str) -> None:
    with SessionLocal() as session:
        job = session.get(SourceJob, job_id)
        run = session.get(ParserRun, run_id)
        if job and run:
            execute_job(session, job, get_settings(), run)


@app.post("/api/parser-jobs/{job_id}/run", response_model=ParserRunRead, status_code=status.HTTP_202_ACCEPTED)
def run_parser_job(
    job_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
) -> ParserRun:
    job = session.get(SourceJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Parser job not found")
    run = ParserRun(job_id=job.id, status="queued")
    session.add(run)
    session.commit()
    session.refresh(run)
    background_tasks.add_task(_run_in_background, job.id, run.id)
    return run


@app.post("/api/parser-runs/{run_id}/resume", response_model=ParserRunRead, status_code=status.HTTP_202_ACCEPTED)
def resume_parser_run(
    run_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
) -> ParserRun:
    run = session.get(ParserRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Parser run not found")
    if run.status not in {"failed", "blocked", "exhausted"}:
        raise HTTPException(status_code=409, detail=f"Parser run with status {run.status!r} cannot be resumed")
    job = session.get(SourceJob, run.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Parser job not found")
    checkpoint = run.checkpoint or {}
    expected = {
        "source": job.source,
        "category": job.category,
        "city": job.city,
        "keywords": job.keywords or [],
    }
    if checkpoint and any(checkpoint.get(key, value) != value for key, value in expected.items()):
        raise HTTPException(status_code=409, detail="Parser job parameters changed after the checkpoint")
    run.status = "queued"
    run.message = None
    run.finished_at = None
    session.commit()
    session.refresh(run)
    background_tasks.add_task(_run_in_background, job.id, run.id)
    return run


@app.get("/api/parser-runs", response_model=list[ParserRunRead])
def parser_runs(limit: int = Query(100, ge=1, le=1000), session: Session = Depends(get_db)) -> list[ParserRun]:
    return list(session.scalars(select(ParserRun).order_by(ParserRun.started_at.desc()).limit(limit)))


@app.get("/api/mail-accounts", response_model=list[MailAccountRead])
def mail_accounts(session: Session = Depends(get_db)) -> list[MailAccount]:
    return list(session.scalars(select(MailAccount).order_by(MailAccount.created_at.desc())))


@app.post("/api/mail-accounts", response_model=MailAccountRead, status_code=201)
def create_mail_account(payload: MailAccountCreate, session: Session = Depends(get_db)) -> MailAccount:
    values = payload.model_dump(exclude={"smtp_password", "imap_password"})
    make_primary = bool(values.get("is_primary")) or not session.scalar(
        select(MailAccount.id).where(MailAccount.is_primary.is_(True)).limit(1)
    )
    values["is_primary"] = make_primary
    if make_primary:
        for existing in session.scalars(select(MailAccount).where(MailAccount.is_primary.is_(True))):
            existing.is_primary = False
    account = MailAccount(
        **values,
        smtp_password_encrypted=encrypt_secret(payload.smtp_password),
        imap_password_encrypted=encrypt_secret(payload.imap_password),
    )
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


@app.patch("/api/mail-accounts/{account_id}", response_model=MailAccountRead)
def update_mail_account(account_id: str, payload: MailAccountUpdate, session: Session = Depends(get_db)) -> MailAccount:
    account = session.get(MailAccount, account_id)
    if not account:
        raise HTTPException(404, "Mail account not found")
    values = payload.model_dump(exclude_unset=True)
    if values.get("is_primary") is True:
        values["active"] = True
        for existing in session.scalars(select(MailAccount).where(
            MailAccount.is_primary.is_(True), MailAccount.id != account_id,
        )):
            existing.is_primary = False
    if values.get("active") is False and account.is_primary:
        values["is_primary"] = False
    if values.pop("smtp_password", None):
        account.smtp_password_encrypted = encrypt_secret(payload.smtp_password or "")
    if values.pop("imap_password", None):
        account.imap_password_encrypted = encrypt_secret(payload.imap_password or "")
    for key, value in values.items():
        setattr(account, key, value)
    session.commit()
    session.refresh(account)
    return account


@app.delete("/api/mail-accounts/{account_id}", status_code=204)
def delete_mail_account(account_id: str, session: Session = Depends(get_db)) -> Response:
    account = session.get(MailAccount, account_id)
    if not account:
        raise HTTPException(404, "Mail account not found")
    if session.scalar(select(EmailDelivery.id).where(EmailDelivery.mailbox_id == account_id).limit(1)):
        raise HTTPException(409, "Mailbox has email history; disable it instead")
    session.delete(account); session.commit()
    return Response(status_code=204)


@app.post("/api/mail-accounts/{account_id}/test-smtp")
def test_mail_account_smtp(account_id: str, session: Session = Depends(get_db)) -> dict[str, bool | str]:
    account = session.get(MailAccount, account_id)
    if not account: raise HTTPException(404, "Mail account not found")
    return diagnose_smtp(account)


@app.post("/api/mail-accounts/{account_id}/test-imap")
def test_mail_account_imap(account_id: str, session: Session = Depends(get_db)) -> dict[str, bool | str]:
    account = session.get(MailAccount, account_id)
    if not account: raise HTTPException(404, "Mail account not found")
    return diagnose_imap(account)


@app.post("/api/mail-accounts/{account_id}/test")
def test_mail_account(account_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    account = session.get(MailAccount, account_id)
    if not account: raise HTTPException(404, "Mail account not found")
    return {"smtp": diagnose_smtp(account), "imap": diagnose_imap(account)}


@app.get("/api/templates", response_model=list[EmailTemplateRead])
def templates(session: Session = Depends(get_db)) -> list[EmailTemplate]:
    return list(session.scalars(select(EmailTemplate).order_by(EmailTemplate.created_at.desc())))


@app.post("/api/templates", response_model=EmailTemplateRead, status_code=201)
def create_template(payload: EmailTemplateCreate, session: Session = Depends(get_db)) -> EmailTemplate:
    template = EmailTemplate(**payload.model_dump())
    session.add(template)
    session.commit()
    session.refresh(template)
    return template


@app.patch("/api/templates/{template_id}", response_model=EmailTemplateRead)
def update_template(template_id: str, payload: EmailTemplateUpdate, session: Session = Depends(get_db)) -> EmailTemplate:
    template = session.get(EmailTemplate, template_id)
    if not template:
        raise HTTPException(404, "Template not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(template, key, value)
    session.commit()
    session.refresh(template)
    return template


@app.delete("/api/templates/{template_id}", status_code=204)
def delete_template(template_id: str, session: Session = Depends(get_db)) -> Response:
    template = session.get(EmailTemplate, template_id)
    if not template: raise HTTPException(404, "Template not found")
    if session.scalar(select(EmailDelivery.id).where(EmailDelivery.template_id == template_id).limit(1)):
        raise HTTPException(409, "Template has email history; disable it instead")
    session.delete(template); session.commit(); return Response(status_code=204)


@app.post("/api/templates/{template_id}/preview")
def preview_template(template_id: str, payload: TemplatePreview, session: Session = Depends(get_db)) -> dict[str, str]:
    template, company = session.get(EmailTemplate, template_id), session.get(Company, payload.company_id)
    if not template or not company: raise HTTPException(404, "Template or company not found")
    subject, html, text = render_template_parts(template, company, sender_template_context(session))
    return {"subject": subject, "html_body": html, "text_body": text}


@app.post("/api/templates/{template_id}/test-send", response_model=DeliveryRead)
def test_send_template(template_id: str, payload: TemplateTestSend, session: Session = Depends(get_db)) -> EmailDelivery:
    template, company, account = session.get(EmailTemplate, template_id), session.get(Company, payload.company_id), session.get(MailAccount, payload.mailbox_id)
    if not template or not company or not account: raise HTTPException(404, "Template, company, or mailbox not found")
    try:
        delivery = build_delivery(session, company, account, template, get_settings(), send_mode="test", recipient_override=payload.recipient_email)
    except ValueError as exc:
        raise HTTPException(409, human_error(exc)) from exc
    process_queue(session, get_settings(), "api-test-send", 1)
    session.refresh(delivery); return delivery


@app.get("/api/proposal-templates")
def proposal_templates(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    ensure_all_proposal_templates(session)
    rows = session.execute(
        select(DirectionProposalTemplate, Direction)
        .join(Direction, Direction.id == DirectionProposalTemplate.direction_id)
        .where(Direction.archived_at.is_(None)).order_by(Direction.name)
    ).all()
    return [serialize_proposal_template(template, direction) for template, direction in rows]


@app.patch("/api/proposal-templates/{template_id}")
def update_proposal_template(
    template_id: str, payload: ProposalTemplateUpdate, session: Session = Depends(get_db),
) -> dict[str, object]:
    template = session.get(DirectionProposalTemplate, template_id)
    if not template:
        raise HTTPException(404, "Шаблон КП не найден.")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(template, key, value)
    template.version += 1
    session.commit()
    session.refresh(template)
    return serialize_proposal_template(template, session.get(Direction, template.direction_id))


@app.post("/api/companies/{company_id}/proposal-drafts", status_code=201)
def create_proposal_draft(
    company_id: str, payload: ProposalDraftCreate, session: Session = Depends(get_db),
) -> dict[str, object]:
    company = session.get(Company, company_id)
    mailbox = session.get(MailAccount, payload.mailbox_id) if payload.mailbox_id else _default_mailbox(session)
    if not company:
        raise HTTPException(404, "Компания не найдена.")
    if not mailbox:
        raise HTTPException(409, "Почтовый ящик не настроен.")
    try:
        draft = prepare_proposal_draft(
            session, company, get_settings(), mailbox=mailbox,
            config=active_sheets_config(session, get_settings()), regenerate=payload.regenerate,
        )
        return serialize_draft(draft)
    except (DeepSeekError, ValueError) as exc:
        raise HTTPException(409, human_error(exc)) from exc


@app.patch("/api/proposal-drafts/{draft_id}")
def save_proposal_draft(
    draft_id: str, payload: ProposalDraftUpdate, session: Session = Depends(get_db),
) -> dict[str, object]:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Черновик КП не найден.")
    if payload.mailbox_id and not session.get(MailAccount, payload.mailbox_id):
        raise HTTPException(404, "Почтовый ящик не найден.")
    try:
        return serialize_draft(update_proposal_draft(session, draft, payload.model_dump(exclude_unset=True), get_settings()))
    except ValueError as exc:
        raise HTTPException(409, human_error(exc)) from exc


@app.post("/api/proposal-drafts/{draft_id}/regenerate")
def regenerate_proposal_draft(draft_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Черновик КП не найден.")
    company = session.get(Company, draft.company_id)
    mailbox = session.get(MailAccount, draft.mailbox_id) if draft.mailbox_id else _default_mailbox(session)
    try:
        refreshed = prepare_proposal_draft(
            session, company, get_settings(), mailbox=mailbox,
            config=session.get(GoogleSheetsConfig, draft.config_id) if draft.config_id else None,
            command_key=draft.command_key, regenerate=True,
        )
        return serialize_draft(refreshed)
    except (DeepSeekError, ValueError) as exc:
        raise HTTPException(409, human_error(exc)) from exc


@app.get("/api/proposal-drafts/{draft_id}/preview")
def preview_proposal_draft(draft_id: str, session: Session = Depends(get_db)) -> dict[str, str]:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    company = session.get(Company, draft.company_id) if draft else None
    if not draft or not company:
        raise HTTPException(404, "Черновик КП не найден.")
    if draft.status == "sent" and draft.sent_html_snapshot:
        return {"subject": draft.subject or "", "html_body": draft.sent_html_snapshot,
                "text_body": draft.sent_text_snapshot or ""}
    return render_proposal(draft, company, get_settings())


@app.get("/proposal-drafts/{draft_id}/preview.html", response_class=HTMLResponse, include_in_schema=False)
def proposal_preview_page(draft_id: str, session: Session = Depends(get_db)) -> HTMLResponse:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    company = session.get(Company, draft.company_id) if draft else None
    if not draft or not company:
        raise HTTPException(404, "Черновик КП не найден.")
    return HTMLResponse(draft.sent_html_snapshot if draft.status == "sent" and draft.sent_html_snapshot
                        else render_proposal(draft, company, get_settings())["html_body"])


@app.post("/api/proposal-drafts/{draft_id}/test-send", response_model=DeliveryRead)
def test_send_proposal(draft_id: str, payload: ProposalTestSend, session: Session = Depends(get_db)) -> EmailDelivery:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Черновик КП не найден.")
    try:
        delivery = send_proposal_draft(
            session, draft, get_settings(), recipient_override=payload.recipient_email, send_mode="test",
        )
        if delivery.status == "queued":
            send_delivery(session, delivery, session.get(MailAccount, delivery.mailbox_id))
        session.refresh(delivery)
        return delivery
    except ValueError as exc:
        raise HTTPException(409, human_error(exc)) from exc


@app.post("/api/proposal-drafts/{draft_id}/confirm-send", response_model=DeliveryRead)
def confirm_send_proposal(draft_id: str, session: Session = Depends(get_db)) -> EmailDelivery:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Черновик КП не найден.")
    try:
        delivery = send_proposal_draft(session, draft, get_settings())
        if delivery.status == "queued":
            account = session.get(MailAccount, delivery.mailbox_id)
            send_delivery(session, delivery, account)
            session.refresh(delivery)
        finalize_proposal_delivery(session, draft, delivery)
        return delivery
    except ValueError as exc:
        raise HTTPException(409, human_error(exc)) from exc


@app.get("/api/campaigns")
def campaigns(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [serialize_campaign(session, row) for row in session.scalars(select(Campaign).order_by(Campaign.created_at.desc()))]


@app.post("/api/campaigns", status_code=201)
def create_campaign(payload: CampaignCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    if not session.get(MailAccount, payload.mailbox_id) or not session.get(EmailTemplate, payload.template_id):
        raise HTTPException(400, "Выберите направление, шаблон и почтовый ящик.")
    template = session.get(EmailTemplate, payload.template_id)
    if payload.direction_id and template.direction_id != payload.direction_id:
        raise HTTPException(409, "Выбранный шаблон относится к другому направлению.")
    campaign = Campaign(**payload.model_dump())
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return serialize_campaign(session, campaign)


@app.patch("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: str, payload: CampaignUpdate, session: Session = Depends(get_db)) -> dict[str, object]:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Автоматическая рассылка не найдена.")
    values = payload.model_dump(exclude_unset=True)
    mailbox_id = values.get("mailbox_id", campaign.mailbox_id)
    template_id = values.get("template_id", campaign.template_id)
    if not session.get(MailAccount, mailbox_id) or not session.get(EmailTemplate, template_id):
        raise HTTPException(400, "Выберите существующий шаблон и почтовый ящик.")
    direction_id = values.get("direction_id", campaign.direction_id)
    template = session.get(EmailTemplate, template_id)
    if direction_id and template.direction_id != direction_id:
        raise HTTPException(409, "Выбранный шаблон относится к другому направлению.")
    for key, value in values.items():
        setattr(campaign, key, value)
    session.commit()
    session.refresh(campaign)
    return serialize_campaign(session, campaign)


@app.post("/api/campaigns/{campaign_id}/run")
def run_campaign(campaign_id: str, session: Session = Depends(get_db)) -> dict[str, int]:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Автоматическая рассылка не найдена.")
    try:
        return execute_campaign(session, campaign, get_settings())
    except ValueError as exc:
        raise HTTPException(409, human_error(exc)) from exc


@app.post("/api/campaigns/{campaign_id}/pause", response_model=CampaignRead)
def pause_campaign(campaign_id: str, session: Session = Depends(get_db)) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if not campaign: raise HTTPException(404, "Campaign not found")
    campaign.status, campaign.active = "paused", False; session.commit(); session.refresh(campaign); return campaign


@app.post("/api/campaigns/{campaign_id}/resume", response_model=CampaignRead)
def resume_campaign(campaign_id: str, session: Session = Depends(get_db)) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if not campaign: raise HTTPException(404, "Campaign not found")
    campaign.status, campaign.active = "running", True; session.commit(); session.refresh(campaign); return campaign


@app.get("/api/campaigns/{campaign_id}/history")
def campaign_history(campaign_id: str, session: Session = Depends(get_db)) -> list[dict[str, object]]:
    if not session.get(Campaign, campaign_id):
        raise HTTPException(404, "Автоматическая рассылка не найдена.")
    return [{
        "id": row.id, "company_id": row.company_id, "recipient_email": row.recipient_email,
        "subject": row.subject, "status": row.status, "created_at": row.created_at, "sent_at": row.sent_at,
    } for row in session.scalars(select(EmailDelivery).where(
        EmailDelivery.campaign_id == campaign_id,
    ).order_by(EmailDelivery.created_at.desc()).limit(500))]


@app.get("/api/sender-settings")
def sender_settings(session: Session = Depends(get_db)) -> dict[str, object]:
    row = get_sender_settings(session); session.commit()
    return serialize_sender(row)


@app.patch("/api/sender-settings")
def save_sender_settings(payload: SenderSettingsUpdate, session: Session = Depends(get_db)) -> dict[str, object]:
    return serialize_sender(update_sender_settings(session, payload.model_dump()))


@app.get("/api/email-journal")
def get_email_journal(
    search: str | None = None, direction_id: str | None = None, status_filter: str | None = None,
    mailbox_id: str | None = None, item_type: str | None = None, days: int | None = Query(None, ge=1, le=3650), session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return email_journal(session, search=search, direction_id=direction_id, status=status_filter,
                         mailbox_id=mailbox_id, item_type=item_type, days=days)


@app.get("/api/deliveries/{delivery_id}/timeline")
def get_delivery_timeline(delivery_id: str, session: Session = Depends(get_db)) -> list[dict[str, object]]:
    if not session.get(EmailDelivery, delivery_id):
        raise HTTPException(404, "Письмо не найдено.")
    return delivery_timeline(session, delivery_id)


@app.get("/api/deliveries", response_model=list[DeliveryRead])
def deliveries(status_filter: str | None = None, mailbox: str | None = None, template: str | None = None, limit: int = Query(100, ge=1, le=1000), session: Session = Depends(get_db)) -> list[EmailDelivery]:
    query = select(EmailDelivery).order_by(EmailDelivery.created_at.desc()).limit(limit)
    if status_filter: query = query.where(EmailDelivery.status == status_filter)
    if mailbox: query = query.where(EmailDelivery.mailbox_id == mailbox)
    if template: query = query.where(EmailDelivery.template_id == template)
    return list(session.scalars(query))


@app.get("/api/deliveries/{delivery_id}/preview")
def delivery_preview(delivery_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    delivery = session.get(EmailDelivery, delivery_id)
    if not delivery:
        raise HTTPException(404, "Письмо не найдено.")
    return {
        "subject": delivery.subject,
        "html_body": delivery.html_body,
        "text_body": delivery.text_body or "",
        "attachments": delivery.attachments_snapshot or [],
    }


@app.get("/deliveries/{delivery_id}/preview.html", response_class=HTMLResponse, include_in_schema=False)
def delivery_preview_page(delivery_id: str, session: Session = Depends(get_db)) -> HTMLResponse:
    delivery = session.get(EmailDelivery, delivery_id)
    if not delivery:
        raise HTTPException(404, "Письмо не найдено.")
    return HTMLResponse(delivery.html_body)


@app.post("/api/companies/{company_id}/send", response_model=DeliveryRead, status_code=202)
def manual_send(company_id: str, payload: ManualSendCreate, session: Session = Depends(get_db)) -> EmailDelivery:
    company, account, template = session.get(Company, company_id), session.get(MailAccount, payload.mailbox_id), session.get(EmailTemplate, payload.template_id)
    if not company or not account or not template: raise HTTPException(404, "Company, mailbox, or template not found")
    try:
        return build_delivery(session, company, account, template, get_settings(), direction_id=payload.direction_id, send_mode="manual", overrides=payload.model_dump(exclude={"mailbox_id", "template_id", "direction_id"}))
    except ValueError as exc: raise HTTPException(409, human_error(exc)) from exc


@app.post("/api/companies/{company_id}/personalization-preview")
def personalization_preview(company_id: str, payload: PersonalizationPreviewCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    company = session.get(Company, company_id)
    template = session.get(EmailTemplate, payload.template_id)
    account = session.get(MailAccount, payload.mailbox_id) if payload.mailbox_id else None
    if not company or not template or (payload.mailbox_id and not account):
        raise HTTPException(404, "Компания, шаблон или почтовый ящик не найден.")
    try:
        return prepare_personalization(session, company, template, get_settings(), account=account, regenerate=payload.regenerate)
    except DeepSeekError as exc:
        raise HTTPException(409 if exc.code in {"not_configured", "disabled", "daily_limit"} else 502, str(exc)) from exc


@app.get("/api/suppressions")
def suppressions(session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [{"email": row.email, "reason": row.reason, "note": row.note, "active": row.active, "created_at": row.created_at} for row in session.scalars(select(Suppression).order_by(Suppression.created_at.desc()))]


@app.post("/api/suppressions", status_code=201)
def add_suppression(payload: SuppressionCreate, session: Session = Depends(get_db)) -> dict[str, object]:
    row = Suppression(email=payload.email.strip().casefold(), reason=payload.reason, note=payload.note, active=True)
    session.merge(row); session.commit(); return {"email": row.email, "reason": row.reason, "active": True}


@app.post("/api/companies/{company_id}/send-personalized", response_model=DeliveryRead)
def personalized_send(company_id: str, payload: PersonalizedSendCreate, session: Session = Depends(get_db)) -> EmailDelivery:
    company = session.get(Company, company_id)
    account = session.get(MailAccount, payload.mailbox_id)
    template = session.get(EmailTemplate, payload.template_id)
    if not company or not account or not template:
        raise HTTPException(404, "Компания, почтовый ящик или шаблон не найден.")
    try:
        return send_personalized(
            session, company, account, template, payload.request_key, get_settings(),
            overrides={"subject": payload.subject, "html_body": payload.html_body, "text_body": payload.text_body},
        )
    except ValueError as exc:
        raise HTTPException(409, human_error(exc)) from exc


@app.get("/api/personalization-drafts/{draft_id}")
def personalization_draft(draft_id: str, session: Session = Depends(get_db)) -> dict[str, object]:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    if not draft:
        raise HTTPException(404, "Готовое персональное письмо не найдено.")
    return serialize_draft(draft)


@app.post("/api/personalization-drafts/{draft_id}/send", response_model=DeliveryRead, status_code=202)
def send_personalization_draft(draft_id: str, payload: PersonalizedSendCreate, session: Session = Depends(get_db)) -> EmailDelivery:
    draft = session.get(SheetPersonalizationDraft, draft_id)
    if not draft or draft.template_id != payload.template_id or draft.mailbox_id != payload.mailbox_id:
        raise HTTPException(409, "Используйте почтовый ящик и шаблон из готового предпросмотра.")
    try:
        return send_sheet_draft(
            session, draft_id, get_settings(),
            overrides={"subject": payload.subject, "html_body": payload.html_body, "text_body": payload.text_body},
        )
    except ValueError as exc:
        raise HTTPException(409, human_error(exc)) from exc


PIXEL = bytes.fromhex("47494638396101000100800000ffffff00000021f90401000000002c00000000010001000002024401003b")


@app.get("/t/open/{token}.gif", include_in_schema=False)
def tracking_pixel(token: str, session: Session = Depends(get_db)) -> Response:
    delivery = session.scalar(select(EmailDelivery).where(EmailDelivery.tracking_token == token))
    if delivery and not delivery.opened_at:
        delivery.opened_at = datetime.now(timezone.utc)
        if delivery.status == "sent":
            delivery.status = "opened"
        record_email_event(session, delivery, "opened")
        session.commit()
        sync_email_event_to_sheets(session, delivery)
    return Response(PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})


@app.get("/t/c/{token}", include_in_schema=False)
def tracking_click(token: str, session: Session = Depends(get_db)) -> RedirectResponse:
    link = session.get(TrackedLink, token)
    if not link:
        raise HTTPException(404, "Link not found")
    delivery = session.get(EmailDelivery, link.delivery_id)
    if delivery:
        first_click = delivery.clicked_at is None
        delivery.clicked_at = delivery.clicked_at or datetime.now(timezone.utc)
        if delivery.status in {"sent", "opened"}:
            delivery.status = "clicked"
        if first_click:
            record_email_event(session, delivery, "clicked", {"target_url": link.target_url})
        session.commit()
        if first_click:
            sync_email_event_to_sheets(session, delivery)
    return RedirectResponse(link.target_url, status_code=302)


def _unsubscribe_delivery(token: str, session: Session) -> tuple[EmailDelivery, bool]:
    delivery = session.scalar(select(EmailDelivery).where(EmailDelivery.unsubscribe_token == token))
    if not delivery:
        raise HTTPException(404, "Delivery not found")
    first_unsubscribe = delivery.unsubscribed_at is None
    delivery.status = "unsubscribed"
    delivery.unsubscribed_at = delivery.unsubscribed_at or datetime.now(timezone.utc)
    session.merge(Suppression(email=delivery.recipient_email, reason="unsubscribe", source_delivery_id=delivery.id, active=True))
    if first_unsubscribe:
        record_email_event(session, delivery, "unsubscribed")
    session.commit()
    if first_unsubscribe:
        sync_email_event_to_sheets(session, delivery)
    return delivery, first_unsubscribe


@app.get("/unsubscribe/{token}", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe(token: str, session: Session = Depends(get_db)) -> str:
    _unsubscribe_delivery(token, session)
    return "<h1>Вы отписаны</h1><p>На этот адрес больше не будут отправляться письма.</p>"


@app.post("/unsubscribe/{token}", include_in_schema=False)
def unsubscribe_one_click(token: str, session: Session = Depends(get_db)) -> Response:
    _unsubscribe_delivery(token, session)
    return Response(status_code=200)


@app.get("/api/google-sheets", response_model=list[GoogleSheetsConfigRead])
def sheets_configs(session: Session = Depends(get_db)) -> list[GoogleSheetsConfig]:
    return list(session.scalars(select(GoogleSheetsConfig)))


@app.post("/api/google-sheets", response_model=GoogleSheetsConfigRead, status_code=201)
def create_sheets_config(payload: GoogleSheetsConfigCreate, session: Session = Depends(get_db)) -> GoogleSheetsConfig:
    config = GoogleSheetsConfig(
        spreadsheet_id=payload.spreadsheet_id,
        worksheet_name=payload.worksheet_name,
        credentials_encrypted=encrypt_secret(json.dumps(payload.service_account_json)),
        active=payload.active,
    )
    session.add(config)
    session.commit()
    session.refresh(config)
    return config


@app.post("/api/google-sheets/{config_id}/sync")
def sync_sheet(config_id: str, session: Session = Depends(get_db)) -> dict[str, int]:
    config = session.get(GoogleSheetsConfig, config_id)
    if not config:
        raise HTTPException(404, "Google Sheets config not found")
    try:
        return sync_companies(session, config)
    except Exception as exc:
        session.rollback()
        session.add(GoogleSyncRun(config_id=config.id, status="failed", error_type=exc.__class__.__name__,
                                  error_message=human_error(exc), finished_at=datetime.now(timezone.utc)))
        session.commit()
        raise HTTPException(502, human_error(exc)) from exc


@app.post("/api/directions/{direction_id}/sync")
def sync_direction_sheet(direction_id: str, session: Session = Depends(get_db)) -> dict[str, int]:
    direction = session.get(Direction, direction_id)
    if not direction:
        raise HTTPException(404, "Direction not found")
    config = _active_sheets_config(session)
    if not config:
        raise HTTPException(409, "Active Google Sheets configuration not found")
    try:
        return GoogleSheetsSyncService(session, config).sync_direction(direction)
    except Exception as exc:
        session.rollback()
        session.add(GoogleSyncRun(config_id=config.id, direction_id=direction.id, status="failed",
                                  error_type=exc.__class__.__name__, error_message=human_error(exc),
                                  finished_at=datetime.now(timezone.utc)))
        session.commit()
        raise HTTPException(502, human_error(exc)) from exc


@app.get("/api/google-sheets/status")
def sheets_status(session: Session = Depends(get_db)) -> dict[str, object]:
    config = _active_sheets_config(session)
    service_account_email = None
    if config:
        try:
            service_account_email = json.loads(decrypt_secret(config.credentials_encrypted)).get("client_email")
        except Exception:
            pass
    return {
        "configured": bool(config),
        "spreadsheet_id": config.spreadsheet_id if config else get_settings().google_sheets_spreadsheet_id,
        "service_account_email": service_account_email,
        "mapped_rows": session.scalar(select(func.count()).select_from(SheetRowMapping).where(SheetRowMapping.spreadsheet_id == config.spreadsheet_id)) if config else 0,
        "last_sync_at": session.scalar(select(func.max(SheetRowMapping.last_synced_at)).where(SheetRowMapping.spreadsheet_id == config.spreadsheet_id)) if config else None,
        "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{config.spreadsheet_id}" if config else None,
    }


@app.post("/api/google-sheets/test")
def test_sheets_connection(session: Session = Depends(get_db)) -> dict[str, object]:
    config = _active_sheets_config(session)
    if not config: raise HTTPException(409, "Active Google Sheets configuration not found")
    direction = session.scalar(select(Direction).where(Direction.archived_at.is_(None)).limit(1))
    if not direction: raise HTTPException(409, "No Direction is configured")
    try:
        worksheet = worksheet_from_config(config, direction.sheet_tab)
        spreadsheet = worksheet.spreadsheet
        return {"connected": True, "spreadsheet_title": spreadsheet.title, "tabs": [item.title for item in spreadsheet.worksheets()]}
    except Exception as exc:
        raise HTTPException(502, human_error(exc)) from exc


@app.get("/api/ai-settings", response_model=list[AIConfigRead])
def ai_settings(session: Session = Depends(get_db)) -> list[AIConfig]:
    return list(session.scalars(select(AIConfig)))


@app.get("/api/ai/status")
def deepseek_status(session: Session = Depends(get_db)) -> dict[str, object]:
    row = get_ai_settings(session)
    session.commit()
    settings = get_settings()
    return {
        "configured": bool(settings.deepseek_api_key),
        "status": "Подключено" if settings.deepseek_api_key else "Не подключено",
        "enrichment_enabled": row.enrichment_enabled,
        "personalization_enabled": row.personalization_enabled,
        "daily_request_limit": row.daily_request_limit,
        "model": settings.deepseek_model,
        "base_url": settings.deepseek_api_base,
    }


@app.patch("/api/ai/settings")
def update_deepseek_settings(payload: AISettingsUpdate, session: Session = Depends(get_db)) -> dict[str, object]:
    row = get_ai_settings(session)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    session.commit()
    return deepseek_status(session)


@app.post("/api/ai/test")
def test_deepseek_connection(session: Session = Depends(get_db)) -> dict[str, object]:
    settings = get_settings()
    try:
        result = DeepSeekClient(session, settings).generate(
            company_id=None, operation="connection_test", content_hash="connection-test", missing_fields=[],
            prompt_version="connection-v1", instructions="Верни только указанный JSON.", input_text='Верни {"subject":"ok","intro":"ok","personalized_paragraph":"ok"}',
            response_model=PersonalizationFragments, max_output_tokens=40, use_cache=False,
        )
        session.commit()
        return {"connected": True, "model": settings.deepseek_model, "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens, "reasoning_tokens": result.usage.reasoning_tokens}
    except DeepSeekError as exc:
        session.commit()
        raise HTTPException(409 if exc.code in {"not_configured", "daily_limit"} else 502, str(exc)) from exc


@app.post("/api/ai-settings", response_model=AIConfigRead, status_code=201)
def create_ai_settings(payload: AIConfigCreate, session: Session = Depends(get_db)) -> AIConfig:
    config = AIConfig(
        api_base=payload.api_base,
        api_key_encrypted=encrypt_secret(payload.api_key),
        model=payload.model,
        prompt_template=payload.prompt_template,
        active=payload.active,
    )
    session.add(config)
    session.commit()
    session.refresh(config)
    return config


def _phrase_background(run_id: str, limit: int) -> None:
    with SessionLocal() as session:
        run = session.get(PhraseSearchRun, run_id)
        if run:
            execute_phrase_search(session, run, get_settings(), limit)


@app.post("/api/phrase-search", response_model=list[PhraseSearchRead], status_code=202)
def phrase_search(payload: PhraseSearchCreate, tasks: BackgroundTasks, session: Session = Depends(get_db)) -> list[PhraseSearchRun]:
    if not session.get(Direction, payload.direction_id):
        raise HTTPException(404, "Направление не найдено.")
    runs = [PhraseSearchRun(direction_id=payload.direction_id, phrase=phrase, city=payload.city,
                            region=payload.region, use_ai=payload.use_ai) for phrase in payload.phrases]
    session.add_all(runs)
    session.commit()
    for run in runs:
        session.refresh(run)
        tasks.add_task(_phrase_background, run.id, payload.limit)
    return runs


@app.get("/api/phrase-search", response_model=list[PhraseSearchRead])
def phrase_search_runs(session: Session = Depends(get_db)) -> list[PhraseSearchRun]:
    return list(session.scalars(select(PhraseSearchRun).order_by(PhraseSearchRun.created_at.desc()).limit(100)))


@app.get("/api/phrase-search/results")
def phrase_search_results(limit: int = Query(200, ge=1, le=500), session: Session = Depends(get_db)) -> list[dict[str, object]]:
    rows = session.execute(select(PhraseSearchResult, PhraseSearchRun, Direction.name).join(
        PhraseSearchRun, PhraseSearchRun.id == PhraseSearchResult.run_id
    ).outerjoin(Direction, Direction.id == PhraseSearchRun.direction_id).order_by(
        PhraseSearchResult.created_at.desc()).limit(limit)).all()
    return [{"id": item.id, "created_at": item.created_at, "direction": direction or "Без направления",
             "phrase": item.phrase, "source_url": item.source_url, "company_id": item.company_id,
             "company_name": item.company_name, "website": item.website, "email": item.email,
             "phone": item.phone, "extraction_method": item.extraction_method, "status": item.status,
             "evidence": item.evidence, "error": item.error} for item, _run, direction in rows]


@app.post("/api/google-sheets/actions/{action}")
def google_sheet_action(action: str, payload: SheetActionRequest, session: Session = Depends(get_db)) -> dict[str, object]:
    if action not in {"prepare", "send"}:
        raise HTTPException(404, "Действие не найдено.")
    try:
        return execute_sheet_action(session, payload.company_id, action, get_settings())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
