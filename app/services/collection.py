from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ParserRun, SourceJob
from app.services.dedup import upsert_lead
from app.services.email_discovery import discover_emails
from app.sources.base import SearchSpec, SourceBlocked
from app.sources.factory import build_adapter


def execute_job(session: Session, job: SourceJob, settings: Settings, run: ParserRun | None = None) -> ParserRun:
    run = run or ParserRun(job_id=job.id)
    session.add(run)
    session.commit()

    requested_scan = int((job.options or {}).get("max_scan", min(settings.source_max_scan, job.limit_new * 10)))
    scan_limit = max(job.limit_new, requested_scan)
    spec = SearchSpec(
        category=job.category,
        city=job.city,
        keywords=job.keywords or [],
        limit=scan_limit,
        options=job.options or {},
    )
    adapter = build_adapter(job.source, settings)
    run.status = "running"
    session.commit()

    resume_external_id = (run.checkpoint or {}).get("last_external_id")
    resume_source_url = (run.checkpoint or {}).get("last_source_url")
    resuming = bool(resume_external_id or resume_source_url)

    try:
        for lead in adapter.collect(spec):
            if resuming:
                reached_checkpoint = (
                    (resume_external_id and lead.source_external_id == resume_external_id)
                    or (resume_source_url and lead.source_url == resume_source_url)
                )
                if reached_checkpoint:
                    resuming = False
                continue
            run.scanned += 1
            enrich_email = (job.options or {}).get(
                "enrich_emails",
                settings.yandex_enrich_emails if job.source == "yandex_maps" else False,
            )
            if lead.website and not lead.email and enrich_email:
                discovered = discover_emails(lead.website)
                if discovered:
                    lead.email = discovered[0].email
                    lead.raw_data["email_discovery"] = [
                        {
                            "email": item.email,
                            "page_url": item.page_url,
                            "confidence": item.confidence,
                            "method": item.method,
                        }
                        for item in discovered
                    ]
            result = upsert_lead(session, lead)
            if result.inserted:
                run.inserted += 1
            else:
                run.duplicates += 1
            run.checkpoint = {
                "last_external_id": lead.source_external_id,
                "last_source_url": lead.source_url,
                "source": job.source,
                "category": job.category,
                "city": job.city,
                "keywords": job.keywords or [],
            }
            session.commit()
            if run.inserted >= job.limit_new:
                break
        run.status = "completed" if run.inserted >= job.limit_new else "exhausted"
    except SourceBlocked as exc:
        session.rollback()
        run = session.get(ParserRun, run.id)
        run.status = "blocked"
        run.errors += 1
        run.message = str(exc)
    except Exception as exc:
        session.rollback()
        run = session.get(ParserRun, run.id)
        run.status = "failed"
        run.errors += 1
        run.message = str(exc)
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run
