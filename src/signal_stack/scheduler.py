from __future__ import annotations

import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from signal_stack.database import SessionLocal, init_db
from signal_stack.config import settings
from signal_stack.models import AutomationSchedule, Company, CrawlRun, Document, ScheduledJobRun, Signal, Source, SourceAudit, utcnow
from signal_stack.services.briefs import generate_brief
from signal_stack.services.intelligence import crawl_approved, crawl_source
from signal_stack.services.security import fetch_public_url
from signal_stack.services.company_research import profile_values
from signal_stack.services.source_discovery import discover_sources, explain_source_relevance


WEEKLY_JOB = "weekly_intelligence_brief"
MONTHLY_JOB = "monthly_signal_stack_audit"
logger = logging.getLogger(__name__)


def _session():
    session = SessionLocal()
    session.info["principal"] = "system:scheduler"
    return session


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def next_weekly(company: Company, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_zone(company.timezone))
    days = (company.brief_weekday - local.weekday()) % 7
    target = local.replace(hour=company.brief_hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
    if target <= local:
        target += timedelta(days=7)
    return target.astimezone(timezone.utc).replace(tzinfo=None)


def next_monthly(company: Company, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_zone(company.timezone))
    year, month = local.year, local.month + 1
    if month == 13:
        year, month = year + 1, 1
    target = local.replace(year=year, month=month, day=1, hour=company.brief_hour, minute=0, second=0, microsecond=0)
    return target.astimezone(timezone.utc).replace(tzinfo=None)


def ensure_schedules(session, company: Company) -> None:
    weekly_signature = f"{company.weekly_enabled}:{company.brief_weekday}:{company.brief_hour}:{company.timezone}"
    monthly_signature = f"{company.monthly_audit_enabled}:{company.brief_hour}:{company.timezone}"
    specs = [
        (WEEKLY_JOB, company.weekly_enabled, 7, next_weekly(company), weekly_signature),
        (MONTHLY_JOB, company.monthly_audit_enabled, 30, next_monthly(company), monthly_signature),
    ]
    for name, enabled, interval, due, signature in specs:
        row = session.scalar(select(AutomationSchedule).where(AutomationSchedule.company_id == company.id, AutomationSchedule.job_name == name))
        if row is None:
            session.add(AutomationSchedule(company_id=company.id, job_name=name, enabled=enabled, interval_days=interval, next_run_at=due, config_signature=signature))
        else:
            changed = row.config_signature != signature
            row.enabled = enabled
            row.interval_days = interval
            if changed and enabled:
                row.next_run_at = due
            row.config_signature = signature


def audit_sources(session, company_id: int) -> dict[str, int]:
    sources = session.scalars(select(Source).where(Source.company_id == company_id, Source.status == "approved")).all()
    checked = healthy = failed = internal = 0
    before_ids = set(session.scalars(select(Source.id).where(Source.company_id == company_id)).all())
    profile = profile_values(session, company_id)
    canonical_seen: dict[str, int] = {}
    for source in sources:
        if source.source_type == "internal":
            internal += 1
            session.add(SourceAudit(company_id=company_id, source_id=source.id, status="internal-only", recommendation="Internal uploads are not fetched from the network; review upload coverage in the internal intelligence page."))
            continue
        prior_check = source.last_checked_at
        try:
            from signal_stack.services.security import normalize_url
            canonical = normalize_url(source.url)
            canonical_seen[canonical] = canonical_seen.get(canonical, 0) + 1
            result = fetch_public_url(source.url)
            if result.status_code == 200:
                source.last_status = "healthy"
                source.last_error = None
                recommendation = "Source responded successfully and passed current robots/access checks; continue monitoring approved content."
                status = "healthy"
                healthy += 1
            else:
                raise RuntimeError(f"HTTP {result.status_code}")
        except Exception as exc:
            source.last_status = "failed"
            source.last_error = str(exc)[:2000]
            recommendation = f"Source check failed: {str(exc)[:400]}. Review reachability, access rules, and continuing relevance before removing it."
            status = "failed"
            failed += 1
        source.last_checked_at = utcnow()
        checked += 1
        session.add(SourceAudit(company_id=company_id, source_id=source.id, status=status, recommendation=recommendation))
        latest_doc = session.scalar(select(Document).where(Document.company_id == company_id, Document.source_id == source.id).order_by(Document.discovered_at.desc(), Document.id.desc()))
        source.relevance, source.relevance_reason = explain_source_relevance(source, profile, content=latest_doc.content if latest_doc else "")
        if source.relevance < .35:
            session.add(SourceAudit(company_id=company_id, source_id=source.id, status="relevance-review", recommendation=f"Current context relevance score is low ({source.relevance:.2f}); review the source fit before renewing approval."))
        if latest_doc and latest_doc.discovered_at < utcnow() - timedelta(days=60):
            session.add(SourceAudit(company_id=company_id, source_id=source.id, status="inactive-review", recommendation="No new document has been collected from this source in the last 60 days of this application's records. Check whether it still updates and remains useful; this says nothing about periods before monitoring began."))
        elif not latest_doc and prior_check and prior_check < utcnow() - timedelta(days=60):
            session.add(SourceAudit(company_id=company_id, source_id=source.id, status="no-evidence-review", recommendation="No document was collected during the source's recorded monitoring period; review its activity and relevance before removal."))
        session.flush()
        recent_audits = session.scalars(select(SourceAudit).where(SourceAudit.company_id == company_id, SourceAudit.source_id == source.id).order_by(SourceAudit.created_at.desc()).limit(3)).all()
        if len(recent_audits) == 3 and all(item.status == "failed" for item in recent_audits):
            session.add(SourceAudit(company_id=company_id, source_id=source.id, status="removal-review", recommendation="The three latest monthly availability checks failed. Consider pausing or removing this source after reviewing its access rules and relevance; no source is removed automatically."))
    for canonical, count in canonical_seen.items():
        if count > 1:
            for source in sources:
                try:
                    from signal_stack.services.security import normalize_url
                    if normalize_url(source.url) == canonical:
                        session.add(SourceAudit(company_id=company_id, source_id=source.id, status="duplicate", recommendation="Another approved source normalizes to the same URL; review and deactivate one copy."))
                except Exception:
                    continue
    discover_sources(session, company_id)
    candidate_rows = session.scalars(select(Source).where(Source.company_id == company_id, Source.status == "candidate")).all()
    new_candidates = [source for source in candidate_rows if source.id not in before_ids]
    for source in new_candidates:
        session.add(SourceAudit(company_id=company_id, source_id=source.id, status="candidate-addition", recommendation="New profile-derived source candidate discovered by the monthly audit. Validate source health, access rules, and relevance before approval."))
    session.commit()
    return {"checked": checked, "healthy": healthy, "failed": failed, "internal": internal, "new_candidates": len(new_candidates)}


def execute_job(company_id: int, job_name: str, *, scheduled_at: datetime | None = None) -> ScheduledJobRun:
    if job_name not in {WEEKLY_JOB, MONTHLY_JOB}:
        raise ValueError("Unknown scheduled job")
    init_db()
    claim_session = None
    try:
        claim_session = _session()
        if claim_session.get_bind().dialect.name == "sqlite":
            claim_session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        stale_before = utcnow() - timedelta(hours=2)
        stale_runs = claim_session.scalars(select(ScheduledJobRun).where(
            ScheduledJobRun.company_id == company_id,
            ScheduledJobRun.job_name == job_name,
            ScheduledJobRun.status == "running",
            ScheduledJobRun.started_at < stale_before,
        )).all()
        for stale in stale_runs:
            stale.status = "failed"
            stale.error = "Run marked failed because its worker stopped reporting for more than two hours."
            stale.ended_at = utcnow()
        if stale_runs:
            claim_session.flush()
        existing = claim_session.scalar(select(ScheduledJobRun).where(
            ScheduledJobRun.company_id == company_id,
            ScheduledJobRun.job_name == job_name,
            ScheduledJobRun.status == "running",
        ))
        if existing:
            raise RuntimeError(f"A {job_name} run is already in progress for this company")
        run = ScheduledJobRun(company_id=company_id, job_name=job_name, scheduled_at=scheduled_at, status="running")
        claim_session.add(run)
        claim_session.commit()
        run_id = run.id
        started_at = run.started_at
    except Exception:
        if claim_session is not None:
            claim_session.rollback()
        raise
    finally:
        if claim_session is not None:
            claim_session.close()

    session = None
    try:
        session = _session()
        if job_name == WEEKLY_JOB:
            crawl_stats = crawl_approved(session, company_id, force=scheduled_at is None)
            brief = generate_brief(session, company_id, days=7)
            signals = session.query(Signal).filter(Signal.company_id == company_id, Signal.created_at >= started_at).count()
            approved_external = session.query(Source).filter(Source.company_id == company_id, Source.status == "approved", Source.source_type != "internal").count()
            run = session.get(ScheduledJobRun, run_id)
            run.documents_processed = crawl_stats["documents"]
            run.signals_generated = signals
            run.summary = f"Approved sources checked: {crawl_stats['sources']}; successful: {crawl_stats['succeeded']}; failed: {crawl_stats['failed']}; new documents: {crawl_stats['documents']}; brief #{brief.id} saved with {brief.signal_count} signals."
            run.status = "partial" if approved_external == 0 else ("succeeded" if crawl_stats["failed"] == 0 else ("partial" if crawl_stats["succeeded"] else "failed"))
            if approved_external == 0:
                run.error = "No approved external sources were available; a coverage-gap brief was saved without inventing signals."
            elif crawl_stats["failed"]:
                run.error = f"{crawl_stats['failed']} source checks failed; see crawl history."
        else:
            stats = audit_sources(session, company_id)
            approved_external = session.query(Source).filter(Source.company_id == company_id, Source.status == "approved", Source.source_type != "internal").count()
            run = session.get(ScheduledJobRun, run_id)
            run.summary = f"Approved network sources checked: {stats['checked']}; healthy: {stats['healthy']}; failed: {stats['failed']}; internal sources skipped: {stats['internal']}; new candidates: {stats['new_candidates']}."
            run.status = "partial" if approved_external == 0 else ("succeeded" if stats["failed"] == 0 else ("partial" if stats["healthy"] else "failed"))
            if approved_external == 0:
                run.error = "No approved external sources were available for a health audit; candidate sources were retained for review."
            elif stats["failed"]:
                run.error = f"{stats['failed']} source health checks failed; review the audit details."
        run.ended_at = utcnow()
        session.commit()
        return run
    except Exception as exc:
        if session is not None:
            session.rollback()
        logger.exception("Scheduled job %s failed for company %s", job_name, company_id)
        failure_session = _session()
        try:
            failed_run = failure_session.get(ScheduledJobRun, run_id)
            if failed_run is None:
                raise RuntimeError(f"Scheduled job run {run_id} disappeared after failure") from exc
            failed_run.status = "failed"
            failed_run.error = str(exc)[:3000]
            failed_run.ended_at = utcnow()
            failure_session.commit()
            return failed_run
        except Exception:
            failure_session.rollback()
            logger.exception("Could not persist failed state for scheduled job run %s", run_id)
            raise
        finally:
            failure_session.close()
    finally:
        if session is not None:
            session.close()


def refresh_due_sources(now: datetime | None = None) -> int:
    """Claim and crawl approved external sources whose configured refresh time has arrived."""
    now = now or utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    scan = _session()
    try:
        source_ids = list(scan.scalars(
            select(Source.id)
            .join(Company, Company.id == Source.company_id)
            .where(Company.active.is_(True), Source.status == "approved", Source.source_type != "internal")
            .order_by(Source.id)
        ).all())
    finally:
        scan.close()

    attempted = 0
    for source_id in source_ids:
        claim = None
        claimed_company_id = None
        interval_hours = 24
        try:
            claim = _session()
            if claim.get_bind().dialect.name == "sqlite":
                claim.connection().exec_driver_sql("BEGIN IMMEDIATE")
            source = claim.get(Source, source_id)
            if source is None or source.status != "approved" or source.source_type == "internal":
                continue
            company = claim.get(Company, source.company_id)
            if company is None or not company.active:
                continue
            interval_hours = max(1, min(720, int(company.source_refresh_hours or 24)))
            if source.refresh_claimed_until and source.refresh_claimed_until > now:
                continue
            stale_claim = source.refresh_claimed_until is not None and source.refresh_claimed_until <= now
            due_at = source.next_due_at
            if due_at is None:
                prior_attempt = source.last_attempted_at
                due_at = prior_attempt + timedelta(hours=interval_hours) if prior_attempt else now
            if not stale_claim and due_at > now:
                continue
            claimed_company_id = source.company_id
            source.last_attempted_at = now
            source.next_due_at = now + timedelta(hours=interval_hours)
            source.refresh_claimed_until = now + timedelta(seconds=max(60, settings.total_request_deadline_seconds + 30))
            claim.commit()
        except Exception:
            if claim is not None:
                claim.rollback()
            logger.exception("Could not claim due source refresh for source %s", source_id)
            continue
        finally:
            if claim is not None:
                claim.close()

        if claimed_company_id is None:
            continue
        execution = _session()
        try:
            run = crawl_source(execution, claimed_company_id, source_id)
            source = execution.get(Source, source_id)
            if source and source.refresh_claimed_until:
                # Covers alternate crawl implementations that return a run without finalizing refresh state.
                finished = run.ended_at or utcnow()
                source.last_checked_at = finished
                source.refresh_claimed_until = None
                if run.status == "succeeded":
                    source.last_success_at = finished
                    source.refresh_failure_count = 0
                    source.next_due_at = finished + timedelta(hours=interval_hours)
                else:
                    source.refresh_failure_count = min(30, source.refresh_failure_count + 1)
                    retry_hours = min(24, interval_hours, 2 ** min(source.refresh_failure_count - 1, 5))
                    source.next_due_at = finished + timedelta(hours=retry_hours)
                execution.commit()
            attempted += 1
        except Exception:
            execution.rollback()
            logger.exception("Due source refresh failed for source %s", source_id)
            recovery = _session()
            try:
                source = recovery.get(Source, source_id)
                if source:
                    finished = utcnow()
                    source.last_attempted_at = finished
                    source.last_checked_at = finished
                    source.refresh_failure_count = min(30, source.refresh_failure_count + 1)
                    retry_hours = min(24, interval_hours, 2 ** min(source.refresh_failure_count - 1, 5))
                    source.next_due_at = finished + timedelta(hours=retry_hours)
                    source.refresh_claimed_until = None
                    source.last_status = "failed"
                    source.last_error = "Scheduled refresh failed; see scheduler logs for details."
                    recovery.commit()
            except Exception:
                recovery.rollback()
                logger.exception("Could not release failed source refresh claim %s", source_id)
            finally:
                recovery.close()
        finally:
            execution.close()
    return attempted


def tick(now: datetime | None = None) -> int:
    now = now or utcnow()
    due: list[tuple[int, str, datetime]] = []
    session = _session()
    try:
        schedules = session.scalars(select(AutomationSchedule).where(AutomationSchedule.enabled.is_(True), AutomationSchedule.next_run_at <= now)).all()
        for schedule in schedules:
            company = session.get(Company, schedule.company_id)
            if not company or not company.active:
                continue
            due.append((company.id, schedule.job_name, schedule.next_run_at))
    finally:
        session.close()
    try:
        completed = refresh_due_sources(now)
    except Exception:
        logger.exception("Due source refresh poll failed")
        completed = 0
    for company_id, job_name, scheduled_at in due:
        try:
            execute_job(company_id, job_name, scheduled_at=scheduled_at)
        except RuntimeError:
            # Another worker owns this company's job; it will advance the due time on completion.
            continue
        session = _session()
        try:
            schedule = session.scalar(select(AutomationSchedule).where(AutomationSchedule.company_id == company_id, AutomationSchedule.job_name == job_name))
            company = session.get(Company, company_id)
            if schedule and company:
                schedule.last_run_at = now
                if job_name == WEEKLY_JOB:
                    schedule.next_run_at = next_weekly(company, now + timedelta(seconds=1))
                else:
                    schedule.next_run_at = next_monthly(company, now + timedelta(seconds=1))
                session.commit()
        except Exception:
            session.rollback()
            logger.exception("Could not advance schedule %s for company %s", job_name, company_id)
            raise
        finally:
            session.close()
        completed += 1
    return completed


def run_forever(poll_seconds: int = 30) -> None:
    init_db()
    while True:
        try:
            tick()
        except Exception:
            logger.exception("Scheduler loop failed")
        time.sleep(max(5, poll_seconds))

