from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models import (
    AIConfig, Campaign, Company, EmailDelivery, EmailTemplate, GoogleSheetsConfig,
    MailAccount, ParserRun, PhraseSearchRun, SourceJob, Suppression, TrackedLink,
)
from app.schemas import (
    AIConfigCreate, AIConfigRead, CampaignCreate, CampaignRead, CampaignUpdate, CompanyRead, CompanyUpdate,
    DeliveryRead, EmailTemplateCreate, EmailTemplateRead, EmailTemplateUpdate, GoogleSheetsConfigCreate,
    GoogleSheetsConfigRead, MailAccountCreate, MailAccountRead, MailAccountUpdate, ParserRunRead,
    PersonalizedSendCreate, PhraseSearchCreate, PhraseSearchRead, SourceJobCreate, SourceJobRead, SourceJobUpdate,
)
from app.services.collection import execute_job
from app.services.google_sheets import sync_companies
from app.services.mailing import execute_campaign, test_imap, test_smtp
from app.services.personalized import send_personalized
from app.services.secrets import encrypt_secret
from app.sources.phrase_search import execute_phrase_search

app = FastAPI(title="LeadFlow", version="0.1.0")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def optional_basic_auth(request: Request, call_next):
    settings = get_settings()
    protected = request.url.path == "/" or request.url.path.startswith(("/api", "/docs", "/redoc", "/openapi"))
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
    return await call_next(request)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def admin_panel() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


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
    }


@app.get("/api/companies", response_model=list[CompanyRead])
def companies(
    city: str | None = None,
    category: str | None = None,
    source: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> list[Company]:
    query = select(Company).order_by(Company.collected_at.desc()).limit(limit).offset(offset)
    if city:
        query = query.where(Company.city == city)
    if category:
        query = query.where(Company.category == category)
    if source:
        query = query.where(Company.source == source)
    return list(session.scalars(query))


@app.patch("/api/companies/{company_id}", response_model=CompanyRead)
def update_company(company_id: str, payload: CompanyUpdate, session: Session = Depends(get_db)) -> Company:
    company = session.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(company, key, value)
    session.commit()
    session.refresh(company)
    return company


@app.get("/api/dashboard")
def dashboard(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    category: str | None = None,
    city: str | None = None,
    source: str | None = None,
    mailbox: str | None = None,
    template: str | None = None,
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
    if values.pop("smtp_password", None):
        account.smtp_password_encrypted = encrypt_secret(payload.smtp_password or "")
    if values.pop("imap_password", None):
        account.imap_password_encrypted = encrypt_secret(payload.imap_password or "")
    for key, value in values.items():
        setattr(account, key, value)
    session.commit()
    session.refresh(account)
    return account


@app.post("/api/mail-accounts/{account_id}/test")
def test_mail_account(account_id: str, session: Session = Depends(get_db)) -> dict[str, str]:
    account = session.get(MailAccount, account_id)
    if not account:
        raise HTTPException(404, "Mail account not found")
    test_smtp(account)
    test_imap(account)
    return {"status": "ok", "smtp": "ok", "imap": "ok"}


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


@app.get("/api/campaigns", response_model=list[CampaignRead])
def campaigns(session: Session = Depends(get_db)) -> list[Campaign]:
    return list(session.scalars(select(Campaign).order_by(Campaign.created_at.desc())))


@app.post("/api/campaigns", response_model=CampaignRead, status_code=201)
def create_campaign(payload: CampaignCreate, session: Session = Depends(get_db)) -> Campaign:
    if not session.get(MailAccount, payload.mailbox_id) or not session.get(EmailTemplate, payload.template_id):
        raise HTTPException(400, "Unknown mailbox or template")
    campaign = Campaign(**payload.model_dump())
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


@app.patch("/api/campaigns/{campaign_id}", response_model=CampaignRead)
def update_campaign(campaign_id: str, payload: CampaignUpdate, session: Session = Depends(get_db)) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    values = payload.model_dump(exclude_unset=True)
    mailbox_id = values.get("mailbox_id", campaign.mailbox_id)
    template_id = values.get("template_id", campaign.template_id)
    if not session.get(MailAccount, mailbox_id) or not session.get(EmailTemplate, template_id):
        raise HTTPException(400, "Unknown mailbox or template")
    for key, value in values.items():
        setattr(campaign, key, value)
    session.commit()
    session.refresh(campaign)
    return campaign


@app.post("/api/campaigns/{campaign_id}/run")
def run_campaign(campaign_id: str, session: Session = Depends(get_db)) -> dict[str, int]:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    return execute_campaign(session, campaign, get_settings())


@app.get("/api/deliveries", response_model=list[DeliveryRead])
def deliveries(limit: int = Query(100, ge=1, le=1000), session: Session = Depends(get_db)) -> list[EmailDelivery]:
    return list(session.scalars(select(EmailDelivery).order_by(EmailDelivery.sent_at.desc().nullslast()).limit(limit)))


@app.post("/api/companies/{company_id}/send-personalized", response_model=DeliveryRead)
def personalized_send(company_id: str, payload: PersonalizedSendCreate, session: Session = Depends(get_db)) -> EmailDelivery:
    company = session.get(Company, company_id)
    account = session.get(MailAccount, payload.mailbox_id)
    template = session.get(EmailTemplate, payload.template_id)
    ai_config = session.get(AIConfig, payload.ai_config_id)
    if not company or not account or not template or not ai_config:
        raise HTTPException(404, "Company, mailbox, template, or AI config not found")
    try:
        return send_personalized(session, company, account, template, ai_config, get_settings())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


PIXEL = bytes.fromhex("47494638396101000100800000ffffff00000021f90401000000002c00000000010001000002024401003b")


@app.get("/t/open/{token}.gif", include_in_schema=False)
def tracking_pixel(token: str, session: Session = Depends(get_db)) -> Response:
    delivery = session.scalar(select(EmailDelivery).where(EmailDelivery.tracking_token == token))
    if delivery and not delivery.opened_at:
        delivery.opened_at = datetime.now(timezone.utc)
        if delivery.status == "sent":
            delivery.status = "opened"
        session.commit()
    return Response(PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})


@app.get("/t/c/{token}", include_in_schema=False)
def tracking_click(token: str, session: Session = Depends(get_db)) -> RedirectResponse:
    link = session.get(TrackedLink, token)
    if not link:
        raise HTTPException(404, "Link not found")
    delivery = session.get(EmailDelivery, link.delivery_id)
    if delivery:
        delivery.clicked_at = delivery.clicked_at or datetime.now(timezone.utc)
        if delivery.status in {"sent", "opened"}:
            delivery.status = "clicked"
        session.commit()
    return RedirectResponse(link.target_url, status_code=302)


@app.get("/unsubscribe/{token}", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe(token: str, session: Session = Depends(get_db)) -> str:
    delivery = session.scalar(select(EmailDelivery).where(EmailDelivery.tracking_token == token))
    if not delivery:
        raise HTTPException(404, "Delivery not found")
    delivery.status = "unsubscribed"
    delivery.unsubscribed_at = datetime.now(timezone.utc)
    session.merge(Suppression(email=delivery.recipient_email, reason="unsubscribe"))
    session.commit()
    return "<h1>Вы отписаны</h1><p>На этот адрес больше не будут отправляться письма.</p>"


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
    return sync_companies(session, config)


@app.get("/api/ai-settings", response_model=list[AIConfigRead])
def ai_settings(session: Session = Depends(get_db)) -> list[AIConfig]:
    return list(session.scalars(select(AIConfig)))


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


@app.post("/api/phrase-search", response_model=PhraseSearchRead, status_code=202)
def phrase_search(payload: PhraseSearchCreate, tasks: BackgroundTasks, session: Session = Depends(get_db)) -> PhraseSearchRun:
    run = PhraseSearchRun(phrase=payload.phrase)
    session.add(run)
    session.commit()
    session.refresh(run)
    tasks.add_task(_phrase_background, run.id, payload.limit)
    return run


@app.get("/api/phrase-search", response_model=list[PhraseSearchRead])
def phrase_search_runs(session: Session = Depends(get_db)) -> list[PhraseSearchRun]:
    return list(session.scalars(select(PhraseSearchRun).order_by(PhraseSearchRun.created_at.desc())))
