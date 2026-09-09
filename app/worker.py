from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Campaign, Direction, DirectionRun, MailAccount, SourceJob
from app.services.collection import execute_job
from app.services.direction_pipeline import execute_direction_pipeline
from app.services.imap_monitor import poll_mailbox
from app.services.mailing import execute_campaign

logging.basicConfig(level=get_settings().log_level)
logger = logging.getLogger(__name__)
scheduler = BlockingScheduler(timezone="UTC")


def run_job(job_id: str) -> None:
    with SessionLocal() as session:
        job = session.get(SourceJob, job_id)
        if job and job.active:
            execute_job(session, job, get_settings())


def refresh_schedules() -> None:
    with SessionLocal() as session:
        jobs = list(session.scalars(select(SourceJob).where(SourceJob.active.is_(True))))
    wanted: set[str] = set()
    for job in jobs:
        if not job.schedule:
            continue
        schedule_id = f"source-job:{job.id}"
        wanted.add(schedule_id)
        try:
            trigger = CronTrigger.from_crontab(job.schedule, timezone="UTC")
        except ValueError as exc:
            logger.error("Invalid cron for job %s: %s", job.id, exc)
            continue
        scheduler.add_job(run_job, trigger, args=[job.id], id=schedule_id, replace_existing=True, max_instances=1)
    for scheduled in scheduler.get_jobs():
        if scheduled.id.startswith("source-job:") and scheduled.id not in wanted:
            scheduler.remove_job(scheduled.id)


def run_direction(direction_id: str) -> None:
    with SessionLocal() as session:
        direction = session.get(Direction, direction_id)
        if direction and direction.active and not direction.archived_at:
            execute_direction_pipeline(session, direction, get_settings())


def refresh_directions() -> None:
    with SessionLocal() as session:
        directions = list(session.scalars(select(Direction).where(
            Direction.active.is_(True), Direction.archived_at.is_(None),
        )))
    wanted: set[str] = set()
    for direction in directions:
        if not direction.schedule:
            continue
        schedule_id = f"direction:{direction.id}"
        wanted.add(schedule_id)
        try:
            trigger = CronTrigger.from_crontab(direction.schedule, timezone="UTC")
        except ValueError as exc:
            logger.error("Invalid cron for direction %s: %s", direction.id, exc)
            continue
        scheduler.add_job(run_direction, trigger, args=[direction.id], id=schedule_id, replace_existing=True, max_instances=1)
    for scheduled in scheduler.get_jobs():
        if scheduled.id.startswith("direction:") and scheduled.id not in wanted:
            scheduler.remove_job(scheduled.id)


def run_campaign(campaign_id: str) -> None:
    with SessionLocal() as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign and campaign.active:
            execute_campaign(session, campaign, get_settings())


def poll_imap() -> None:
    with SessionLocal() as session:
        accounts = list(session.scalars(select(MailAccount).where(MailAccount.active.is_(True))))
        for account in accounts:
            try:
                poll_mailbox(session, account, get_settings().manager_email)
            except Exception:
                logger.exception("IMAP polling failed for %s", account.id)


def refresh_campaigns() -> None:
    with SessionLocal() as session:
        campaigns = list(session.scalars(select(Campaign).where(Campaign.active.is_(True))))
    wanted: set[str] = set()
    for campaign in campaigns:
        if not campaign.schedule:
            continue
        schedule_id = f"campaign:{campaign.id}"
        wanted.add(schedule_id)
        try:
            trigger = CronTrigger.from_crontab(campaign.schedule, timezone="UTC")
        except ValueError as exc:
            logger.error("Invalid campaign cron for %s: %s", campaign.id, exc)
            continue
        scheduler.add_job(run_campaign, trigger, args=[campaign.id], id=schedule_id, replace_existing=True, max_instances=1)
    for scheduled in scheduler.get_jobs():
        if scheduled.id.startswith("campaign:") and scheduled.id not in wanted:
            scheduler.remove_job(scheduled.id)


def main() -> None:
    refresh_schedules()
    refresh_directions()
    refresh_campaigns()
    scheduler.add_job(refresh_schedules, "interval", seconds=60, id="refresh", replace_existing=True)
    scheduler.add_job(refresh_directions, "interval", seconds=60, id="refresh-directions", replace_existing=True)
    scheduler.add_job(refresh_campaigns, "interval", seconds=60, id="refresh-campaigns", replace_existing=True)
    scheduler.add_job(poll_imap, "interval", minutes=5, id="imap", replace_existing=True, max_instances=1)
    scheduler.start()


if __name__ == "__main__":
    main()
