from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base
from app.models import Company, SourceJob
from app.services.collection import execute_job
from app.services.dedup import upsert_lead
from app.sources.base import CompanyLead, SearchSpec, SourceAdapter, SourceBlocked


class FakeAdapter(SourceAdapter):
    name = "fake"

    def collect(self, spec: SearchSpec):
        yield CompanyLead(source="yandex_maps", source_external_id="old-elsewhere", source_url=None,
                          company_name="Duplicate", phone="8 999 100-20-30")
        yield CompanyLead(source="yandex_maps", source_external_id="new-1", source_url=None,
                          company_name="New one", phone="+7 999 000-00-01")
        yield CompanyLead(source="yandex_maps", source_external_id="new-2", source_url=None,
                          company_name="New two", phone="+7 999 000-00-02")


def test_limit_new_does_not_count_duplicates(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        upsert_lead(session, CompanyLead(source="two_gis", source_external_id="old", source_url=None,
                                         company_name="Existing", phone="+7 999 100-20-30"))
        session.commit()
        job = SourceJob(source="yandex_maps", category="test", city="Москва", limit_new=2, options={})
        session.add(job)
        session.commit()
        monkeypatch.setattr("app.services.collection.build_adapter", lambda *_: FakeAdapter())
        run = execute_job(session, job, Settings(database_url="sqlite+pysqlite:///:memory:", source_max_scan=20))
        assert run.status == "completed"
        assert run.scanned == 3
        assert run.inserted == 2
        assert run.duplicates == 1


class InterruptedAdapter(SourceAdapter):
    name = "fake"

    def collect(self, spec: SearchSpec):
        yield CompanyLead(source="yandex_maps", source_external_id="resume-1", source_url="https://maps/1",
                          company_name="Resume one", phone="+7 999 000-01-01")
        raise RuntimeError("interrupted")


class ResumedAdapter(SourceAdapter):
    name = "fake"

    def collect(self, spec: SearchSpec):
        yield CompanyLead(source="yandex_maps", source_external_id="resume-1", source_url="https://maps/1",
                          company_name="Resume one", phone="+7 999 000-01-01")
        yield CompanyLead(source="yandex_maps", source_external_id="resume-2", source_url="https://maps/2",
                          company_name="Resume two", phone="+7 999 000-01-02")


def test_resume_uses_checkpoint(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job = SourceJob(source="yandex_maps", category="test", city="Москва", limit_new=2, options={})
        session.add(job)
        session.commit()
        monkeypatch.setattr("app.services.collection.build_adapter", lambda *_: InterruptedAdapter())
        run = execute_job(session, job, Settings(database_url="sqlite+pysqlite:///:memory:"))
        assert run.status == "failed"
        assert run.inserted == 1
        assert run.checkpoint["last_external_id"] == "resume-1"

        monkeypatch.setattr("app.services.collection.build_adapter", lambda *_: ResumedAdapter())
        run = execute_job(session, job, Settings(database_url="sqlite+pysqlite:///:memory:"), run)
        assert run.status == "completed"
        assert run.inserted == 2
        assert run.scanned == 2
        assert session.query(Company).count() == 2


class EmptyAdapter(SourceAdapter):
    name = "fake"

    def collect(self, spec: SearchSpec):
        return iter(())


class BlockedAdapter(SourceAdapter):
    name = "fake"

    def collect(self, spec: SearchSpec):
        raise SourceBlocked("captcha")


def test_terminal_statuses(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        job = SourceJob(source="yandex_maps", category="test", city="Москва", limit_new=1, options={})
        session.add(job)
        session.commit()

        monkeypatch.setattr("app.services.collection.build_adapter", lambda *_: EmptyAdapter())
        assert execute_job(session, job, Settings(database_url="sqlite+pysqlite:///:memory:")).status == "exhausted"

        monkeypatch.setattr("app.services.collection.build_adapter", lambda *_: BlockedAdapter())
        assert execute_job(session, job, Settings(database_url="sqlite+pysqlite:///:memory:")).status == "blocked"
