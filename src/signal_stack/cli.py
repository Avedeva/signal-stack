from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from sqlalchemy import select

from signal_stack.database import SessionLocal, init_db
from signal_stack.config import settings
from signal_stack.models import Brief, Company, Document, ScheduledJobRun, Signal, Source
from signal_stack.scheduler import MONTHLY_JOB, WEEKLY_JOB, ensure_schedules, execute_job, run_forever
from signal_stack.services.company_research import create_company, research_company, save_profile_value, save_profile_version
from signal_stack.services.briefs import export_pdf
from signal_stack.services.competitor_discovery import discover_competitors_from_documents
from signal_stack.services.intelligence import crawl_approved
from signal_stack.services.interview import start_interview
from signal_stack.services.security import normalize_url
from signal_stack.services.source_discovery import discover_sources, set_source_status, upsert_source


DEMO_COMPANIES = [
    {"name": "MeetUniversity", "website": "https://www.meetuniversity.com", "industry": None},
    {"name": "Zerodha", "website": "https://zerodha.com", "industry": "Fintech"},
    {"name": "Zomato", "website": "https://www.zomato.com", "industry": "Food delivery"},
]


def _session():
    session = SessionLocal()
    session.info["principal"] = "system:cli"
    return session


def _company(session, company_id: int) -> Company:
    company = session.get(Company, company_id)
    if not company:
        raise ValueError(f"Company {company_id} does not exist")
    return company


def bootstrap_demo(do_research: bool) -> None:
    init_db()
    session = _session()
    try:
        for spec in DEMO_COMPANIES:
            company = create_company(session, spec["name"], spec["website"])
            if spec["industry"]:
                save_profile_value(session, company.id, "industry", spec["industry"], status="assignment-provided")
            discover_sources(session, company.id)
            ensure_schedules(session, company)
            session.commit()
            print(f"Configured {company.name} (id={company.id}, website={company.website}).")
            if do_research:
                try:
                    result = research_company(session, company)
                    print(f"  Research observations: {json.dumps(result, ensure_ascii=False)}")
                except Exception as exc:
                    print(f"  Research failed honestly: {exc}")
                discover_sources(session, company.id)
        print("\nDatabase counts (no synthetic records):")
        for company in session.scalars(select(Company).order_by(Company.name)).all():
            sources = session.query(Source).filter_by(company_id=company.id).count()
            approved = session.query(Source).filter_by(company_id=company.id, status="approved").count()
            docs = session.query(Document).filter_by(company_id=company.id).count()
            signals = session.query(Signal).filter_by(company_id=company.id).count()
            print(f"  {company.name}: {sources} sources ({approved} approved), {docs} documents, {signals} signals")
        print("Candidates remain unapproved until they pass source relevance, access, and health review.")
    finally:
        session.close()


def import_profile_evidence(path: str) -> None:
    """Import cited first-party profile facts without creating crawler documents or signals."""
    init_db()
    fixture_path = Path(path)
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    session = _session()
    try:
        for spec in payload.get("companies", []):
            name = spec["name"].strip()
            website = normalize_url(spec["website"])
            company = session.scalar(select(Company).where(Company.website == website))
            if company is None:
                company = create_company(session, name, website)
            source_url = normalize_url(spec["source_url"])
            for fact in spec.get("facts", []):
                save_profile_value(
                    session, company.id, fact["key"], fact["value"],
                    status=fact.get("status", "browser-stated"),
                    evidence_url=source_url,
                    evidence_snippet=fact.get("snippet"),
                )
            upsert_source(
                session, company.id, url=source_url, title=spec.get("source_title", "First-party company information"),
                category="market", source_type="website",
                reason=f"First-party profile reference reviewed through browser research on {payload.get('retrieved_on', 'an unspecified date')}; the app's live access/robots check is still required and this source is not approved.",
                reliability=.7, relevance=.8,
            )
            save_profile_version(session, company.id, f"browser-assisted profile reference ({payload.get('retrieved_on', 'date unknown')}; not app-crawled)")
            discover_sources(session, company.id)
            interview = start_interview(session, company.id)
            session.commit()
            print(f"{company.name}: {len(spec.get('facts', []))} source-attributed profile fields; {len(interview.questions)} interview questions; public sources remain candidates.")
        print("Imported profile evidence only. No crawled document, signal, or public-source approval was created.")
    finally:
        session.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="signal-stack")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create missing database tables without dropping existing data")
    bootstrap = commands.add_parser("bootstrap-demo", help="Configure the three requested demo companies")
    bootstrap.add_argument("--research", action="store_true", help="Attempt live homepage research; failures are recorded")
    evidence = commands.add_parser("import-profile-evidence", help="Import the cited, browser-reviewed first-party profile fixture")
    evidence.add_argument("--path", default="demo_evidence/company_profiles.json")
    for name, help_text in [
        ("research", "Research the company's public homepage"),
        ("discover", "Generate context-based source candidates"),
        ("crawl", "Crawl approved sources and save evidence-backed records"),
        ("competitors", "Extract explicit competitor candidates from collected documents"),
        ("weekly", "Run the weekly collect-and-brief job now"),
        ("audit", "Run the monthly source-health audit now"),
    ]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--company-id", type=int, required=True)
    status = commands.add_parser("source-status", help="Change an existing source's review status")
    status.add_argument("--company-id", type=int, required=True)
    status.add_argument("--source-id", type=int, required=True)
    status.add_argument("--status", choices=["approved", "candidate", "rejected", "paused", "removed"], required=True)
    export_command = commands.add_parser("export-brief", help="Export a saved brief as Markdown and PDF")
    export_command.add_argument("--company-id", type=int, required=True)
    export_command.add_argument("--brief-id", type=int)
    scheduler = commands.add_parser("scheduler", help="Run the independent persistent local scheduler")
    scheduler.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args(argv)
    init_db()
    if args.command == "init-db":
        print("Database initialized; existing rows were not dropped.")
        return
    if args.command == "bootstrap-demo":
        bootstrap_demo(args.research)
        return
    if args.command == "import-profile-evidence":
        import_profile_evidence(args.path)
        return
    if args.command == "scheduler":
        print(f"Scheduler active; polling every {max(5, args.poll_seconds)} seconds. Keep this process running.", flush=True)
        run_forever(args.poll_seconds)
        return
    session = _session()
    try:
        company = _company(session, args.company_id)
        if args.command == "export-brief":
            query = select(Brief).where(Brief.company_id == company.id)
            if args.brief_id:
                query = query.where(Brief.id == args.brief_id)
            brief = session.scalar(query.order_by(Brief.created_at.desc()))
            if not brief:
                raise ValueError("No matching saved brief exists for this company")
            settings.prepare()
            slug = re.sub(r"[^a-z0-9]+", "-", company.name.lower()).strip("-") or f"company-{company.id}"
            markdown_path = settings.reports_dir / f"{slug}-brief-{brief.id}.md"
            pdf_path = settings.reports_dir / f"{slug}-brief-{brief.id}.pdf"
            markdown_path.write_text(brief.markdown, encoding="utf-8")
            pdf_path.write_bytes(export_pdf(brief.markdown))
            print(f"Exported brief #{brief.id}: {markdown_path} and {pdf_path}")
        elif args.command == "research":
            print(json.dumps(research_company(session, company), ensure_ascii=False, indent=2))
        elif args.command == "discover":
            candidates = discover_sources(session, company.id)
            print(f"Generated {len(candidates)} source candidates. No candidate was approved.")
        elif args.command == "source-status":
            source = set_source_status(session, args.company_id, args.source_id, args.status)
            print(f"Source #{source.id} status: {source.status}")
        elif args.command == "crawl":
            totals = crawl_approved(session, company.id, force=True)
            print(json.dumps(totals, indent=2))
        elif args.command == "competitors":
            found = discover_competitors_from_documents(session, company.id)
            print(f"Added {len(found)} cited, unverified competitor candidates.")
        elif args.command in {"weekly", "audit"}:
            job_name = WEEKLY_JOB if args.command == "weekly" else MONTHLY_JOB
            result = execute_job(company.id, job_name)
            print(f"Job #{result.id}: {result.status}")
            print(result.summary or result.error or "No summary")
    finally:
        session.close()


if __name__ == "__main__":
    main()

