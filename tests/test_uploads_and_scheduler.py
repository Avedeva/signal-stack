import io
import zipfile
from xml.etree import ElementTree as ET
from dataclasses import replace

import pytest
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy.orm import sessionmaker

from signal_stack import scheduler
from signal_stack.models import Company, ScheduledJobRun, Source, utcnow
from signal_stack.services.company_research import create_company
from signal_stack.services.file_processing import _extract, ingest_upload


def _xlsx_bytes():
    output = io.BytesIO()
    files = {
        "xl/workbook.xml": b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sales" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>',
        "xl/worksheets/sheet1.xml": b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Reason</t></is></c><c r="B1" t="inlineStr"><is><t>Count</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Price concern</t></is></c><c r="B2"><v>3</v></c></row></sheetData></worksheet>',
    }
    with zipfile.ZipFile(output, "w") as package:
        for path, content in files.items():
            package.writestr(path, content)
    return output.getvalue()


def _pdf_bytes():
    output = io.BytesIO()
    canvas = Canvas(output)
    canvas.drawString(50, 750, "Support tickets mention delivery delays")
    canvas.save()
    return output.getvalue()


def test_upload_parsers_cover_csv_xlsx_and_pdf():
    assert "Price concern" in _extract("feedback.csv", b"topic,count\nPrice concern,3")
    assert "Price concern" in _extract("sales.xlsx", _xlsx_bytes())
    assert "delivery delays" in _extract("support.pdf", _pdf_bytes())
    with pytest.raises(ValueError, match="Supported uploads"):
        _extract("image.png", b"data")
    with pytest.raises(ValueError, match="signature"):
        _extract("bad.pdf", b"not a pdf")


def test_internal_upload_becomes_company_scoped_evidence(db_session, monkeypatch, tmp_path):
    from signal_stack import config
    import signal_stack.services.file_processing as file_processing
    monkeypatch.setattr(file_processing, "settings", replace(config.settings, data_dir=tmp_path / "data"))
    company = create_company(db_session, "Upload Co", "https://upload.example")
    row, count, signals = ingest_upload(db_session, company.id, "..\\feedback.csv", b"objection,count\nCustomer price objection,4\nSupport issue delay,2")
    assert row.filename == "feedback.csv"
    assert row.status == "processed"
    assert count == 1
    assert signals == 1
    assert row.company_id == company.id
    assert db_session.query(__import__("signal_stack.models", fromlist=["Signal"]).Signal).filter_by(company_id=company.id).count() == 1


def test_internal_upload_without_detected_theme_remains_document_not_signal(db_session, monkeypatch, tmp_path):
    from signal_stack import config
    import signal_stack.services.file_processing as file_processing
    from signal_stack.models import Document, Signal
    monkeypatch.setattr(file_processing, "settings", replace(config.settings, data_dir=tmp_path / "data"))
    company = create_company(db_session, "Quiet Upload Co", "https://quiet-upload.example")
    _row, docs, signals = ingest_upload(db_session, company.id, "notes.txt", b"General notes about the project and next meeting.")
    assert docs == 1
    assert signals == 0
    assert db_session.query(Document).filter_by(company_id=company.id).count() == 1
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 0


def test_persistent_scheduler_runs_and_logs_success_and_failure(db_session, monkeypatch):
    from signal_stack.database import Base
    from signal_stack.models import Signal
    from signal_stack.scheduler import WEEKLY_JOB, MONTHLY_JOB
    bind = db_session.get_bind()
    factory = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    company = create_company(db_session, "Scheduled Co", "https://scheduled.example")
    db_session.add(Source(company_id=company.id, url="https://scheduled.example/feed", title="Approved feed", category="market", source_type="rss", reason="test fixture source", status="approved", reliability=.5))
    db_session.commit()
    monkeypatch.setattr(scheduler, "crawl_approved", lambda session, company_id, force=False: {"sources": 2, "succeeded": 2, "failed": 0, "documents": 3})
    monkeypatch.setattr(scheduler, "generate_brief", lambda session, company_id, days=7: type("BriefStub", (), {"id": 22, "signal_count": 0})())
    weekly = scheduler.execute_job(company.id, WEEKLY_JOB)
    assert weekly.status == "succeeded"
    assert "brief #22" in weekly.summary
    busy = ScheduledJobRun(company_id=company.id, job_name=WEEKLY_JOB, status="running")
    db_session.add(busy)
    db_session.commit()
    with pytest.raises(RuntimeError, match="already in progress"):
        scheduler.execute_job(company.id, WEEKLY_JOB)
    db_session.delete(busy)
    db_session.commit()
    monkeypatch.setattr(scheduler, "audit_sources", lambda session, company_id: {"checked": 1, "healthy": 1, "failed": 0, "internal": 0, "new_candidates": 2})
    monthly = scheduler.execute_job(company.id, MONTHLY_JOB)
    assert monthly.status == "succeeded"
    monkeypatch.setattr(scheduler, "crawl_approved", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated failure")))
    failed = scheduler.execute_job(company.id, WEEKLY_JOB)
    assert failed.status == "failed"
    assert "simulated failure" in failed.error
    assert db_session.query(ScheduledJobRun).filter_by(company_id=company.id).count() == 3


def test_scheduler_computes_local_timezone_runs(db_session):
    from signal_stack.scheduler import next_weekly
    from datetime import datetime, timezone
    company = Company(name="Schedule", website="https://schedule.example", timezone="Asia/Kolkata", brief_weekday=0, brief_hour=8)
    # Sunday evening UTC is Monday early morning in Kolkata.
    run = next_weekly(company, datetime(2026, 11, 8, 22, 0, tzinfo=timezone.utc))
    assert run.weekday() == 0


def test_scheduler_handles_nonexistent_and_ambiguous_daylight_saving_times():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from signal_stack.scheduler import next_weekly

    spring = Company(name="Spring", website="https://spring.example", timezone="America/New_York", brief_weekday=6, brief_hour=2)
    spring_run = next_weekly(spring, datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc))
    spring_local = spring_run.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    assert spring_local.isoformat() == "2026-03-08T03:00:00-04:00"

    fall = Company(name="Fall", website="https://fall.example", timezone="America/New_York", brief_weekday=6, brief_hour=1)
    fall_run = next_weekly(fall, datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
    fall_local = fall_run.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    assert fall_local.isoformat() == "2026-11-01T01:00:00-04:00"


def test_monthly_audit_recommends_review_after_three_availability_failures(db_session, monkeypatch):
    from signal_stack.services.source_discovery import discover_sources
    company = create_company(db_session, "Audit Co", "https://audit.example")
    source = discover_sources(db_session, company.id)[0]
    source.status = "approved"
    db_session.commit()

    def fail(*args, **kwargs):
        raise RuntimeError("host unavailable")

    monkeypatch.setattr(scheduler, "fetch_public_url", fail)
    for _ in range(3):
        stats = scheduler.audit_sources(db_session, company.id)
    assert stats["failed"] == 1
    reviews = db_session.query(__import__("signal_stack.models", fromlist=["SourceAudit"]).SourceAudit).filter_by(source_id=source.id, status="removal-review").count()
    assert reviews == 1
    assert source.status == "approved"  # Audit recommends review; it never silently removes sources.


@pytest.mark.parametrize("interval_hours", [1, 24, 168, 720])
def test_scheduler_refreshes_due_sources_at_configured_hourly_to_monthly_intervals(db_session, monkeypatch, interval_hours):
    from datetime import timedelta
    from signal_stack.models import CrawlRun
    from signal_stack.services import intelligence
    from signal_stack.services.security import FetchResult

    now = utcnow()
    company = create_company(db_session, f"Refresh {interval_hours}", f"https://refresh-{interval_hours}.example")
    company.source_refresh_hours = interval_hours
    source = Source(company_id=company.id, url=f"https://refresh-{interval_hours}.example/feed", title="Feed", category="market", source_type="rss", reason="fixture", status="approved")
    db_session.add(source)
    db_session.commit()
    monkeypatch.setattr(scheduler, "SessionLocal", sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(intelligence, "utcnow", lambda: now)
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, b"<rss><channel><item><title>Annual report</title><link>https://publisher.example/annual</link><description>A public report with substantive business market evidence.</description><pubDate>Thu, 12 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"))

    assert scheduler.refresh_due_sources(now) == 1
    db_session.refresh(source)
    assert source.last_attempted_at == now
    assert source.last_success_at == now
    assert source.next_due_at == now + timedelta(hours=interval_hours)
    assert source.refresh_claimed_until is None
    assert scheduler.refresh_due_sources(source.next_due_at - timedelta(minutes=1)) == 0
    assert scheduler.refresh_due_sources(source.next_due_at) == 1


def test_failed_refresh_uses_bounded_exponential_retry_and_records_attempt(db_session, monkeypatch):
    from datetime import timedelta
    from signal_stack.services import intelligence

    clock = [utcnow()]
    company = create_company(db_session, "Retry Co", "https://retry.example")
    company.source_refresh_hours = 24
    source = Source(company_id=company.id, url="https://retry.example/feed", title="Feed", category="market", source_type="rss", reason="fixture", status="approved")
    db_session.add(source)
    db_session.commit()
    monkeypatch.setattr(scheduler, "SessionLocal", sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False))
    monkeypatch.setattr(intelligence, "utcnow", lambda: clock[0])
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda _url: (_ for _ in ()).throw(RuntimeError("temporary network failure")))

    assert scheduler.refresh_due_sources(clock[0]) == 1
    db_session.refresh(source)
    assert source.last_success_at is None
    assert source.refresh_failure_count == 1
    assert source.next_due_at == clock[0] + timedelta(hours=1)
    assert source.last_status == "failed"
    assert scheduler.refresh_due_sources(clock[0] + timedelta(minutes=30)) == 0
    clock[0] += timedelta(hours=1)
    assert scheduler.refresh_due_sources(clock[0]) == 1
    db_session.refresh(source)
    assert source.refresh_failure_count == 2
    assert source.next_due_at == clock[0] + timedelta(hours=2)


def test_concurrent_scheduler_polls_claim_a_source_only_once(db_session, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy.orm import sessionmaker
    from signal_stack.models import CrawlRun

    now = utcnow()
    company = create_company(db_session, "Concurrent Refresh Co", "https://concurrent-refresh.example")
    source = Source(company_id=company.id, url="https://concurrent-refresh.example/feed", title="Feed", category="market", source_type="rss", reason="fixture", status="approved")
    db_session.add(source)
    db_session.commit()
    monkeypatch.setattr(scheduler, "SessionLocal", sessionmaker(bind=db_session.get_bind(), autoflush=False, expire_on_commit=False))
    calls = []
    call_lock = threading.Lock()

    def slow_crawl(_session, company_id, source_id):
        with call_lock:
            calls.append(source_id)
        time.sleep(.15)
        return CrawlRun(company_id=company_id, source_id=source_id, status="succeeded", ended_at=now)

    monkeypatch.setattr(scheduler, "crawl_source", slow_crawl)
    barrier = threading.Barrier(2)
    def poll():
        barrier.wait()
        return scheduler.refresh_due_sources(now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: poll(), range(2)))
    assert sorted(results) == [0, 1]
    assert calls == [source.id]


def test_scheduler_closes_session_when_transaction_acquisition_fails(db_session, monkeypatch):
    from sqlalchemy.orm import Session

    closed = []
    class BrokenConnectionSession(Session):
        def connection(self, *args, **kwargs):
            raise RuntimeError("transaction acquisition failed")
        def close(self):
            closed.append(True)
            super().close()

    factory = sessionmaker(bind=db_session.get_bind(), class_=BrokenConnectionSession, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    with pytest.raises(RuntimeError, match="transaction acquisition failed"):
        scheduler.execute_job(999, scheduler.WEEKLY_JOB)
    assert closed == [True]


def test_scheduler_surfaces_session_creation_failure_without_leaking_session(monkeypatch):
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    monkeypatch.setattr(scheduler, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("session creation failed")))
    with pytest.raises(RuntimeError, match="session creation failed"):
        scheduler.execute_job(999, scheduler.WEEKLY_JOB)


def test_scheduler_persists_failed_run_and_closes_sessions_when_commit_fails(db_session, monkeypatch):
    from sqlalchemy.orm import Session

    company = create_company(db_session, "Commit Failure Co", "https://commit-failure.example")
    commits = [0]
    closed = []
    class FailingCommitSession(Session):
        def commit(self):
            commits[0] += 1
            if commits[0] == 2:
                raise RuntimeError("commit failed")
            return super().commit()
        def close(self):
            closed.append(True)
            super().close()

    factory = sessionmaker(bind=db_session.get_bind(), class_=FailingCommitSession, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(scheduler, "SessionLocal", factory)
    monkeypatch.setattr(scheduler, "init_db", lambda: None)
    monkeypatch.setattr(scheduler, "crawl_approved", lambda *args, **kwargs: {"sources": 0, "succeeded": 0, "failed": 0, "documents": 0})
    monkeypatch.setattr(scheduler, "generate_brief", lambda *args, **kwargs: type("BriefStub", (), {"id": 10, "signal_count": 0})())

    result = scheduler.execute_job(company.id, scheduler.WEEKLY_JOB)

    assert result.status == "failed"
    assert "commit failed" in result.error
    assert commits[0] == 3
    assert len(closed) == 3

