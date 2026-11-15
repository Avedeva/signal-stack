from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from signal_stack.config import settings
from signal_stack.auth import authenticate_streamlit, logout_streamlit
from signal_stack.database import SessionLocal, init_db
from signal_stack.models import (
    AutomationSchedule, Brief, BriefAction, Company, CompanyClassification, Competitor, CrawlRun, Development, Document, InterviewQuestion, OnboardingStage,
    InterviewSession, ProfileAttribute, ProfileVersion, ScheduledJobRun, Signal, SignalEvidence, Source, SourceAudit,
    UploadedFile, utcnow,
)
from signal_stack.scheduler import MONTHLY_JOB, WEEKLY_JOB, ensure_schedules, execute_job
from signal_stack.services.briefs import brief_history, export_pdf, generate_brief, sanitize_persisted_brief
from signal_stack.services.competitor_discovery import discover_competitors_from_documents, update_competitor
from signal_stack.services.company_deletion import delete_company
from signal_stack.services.classification import classify_company_context
from signal_stack.services.company_research import create_company, profile_values, research_company, save_profile_value, save_profile_version
from signal_stack.services.file_processing import ALLOWED_EXTENSIONS, ingest_upload
from signal_stack.services.internal_analysis import SEMANTICS, read_business_table
from signal_stack.services.intelligence import crawl_source, recent_signals
from signal_stack.services.interview import current_question, start_interview, submit_answer
from signal_stack.services.onboarding import run_onboarding
from signal_stack.services.public_research import configured_search_provider, discover_public_research_sources
from signal_stack.services.ranking import DEFAULT_RANKING_WEIGHTS, load_ranking_weights, save_ranking_weights
from signal_stack.services.security import normalize_url
from signal_stack.services.source_discovery import add_competitor, approve_validated_sources, discover_sources, reassess_sources, set_source_status, upsert_source, validate_source
from signal_stack.services.verification import transition_verification, verification_history


NAVIGATION = [
    "Home / Overview", "Company Setup", "Company Intelligence Profile", "Adaptive AI Interview",
    "Competitors", "Signal Stack", "Source Manager", "Intelligence Feed", "Monday Morning Brief",
    "Brief History", "Internal File Upload", "Automation / Job History", "Settings",
]


def _fmt_datetime(value):
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "Never"


def _page_home(session, company):
    st.subheader("Company intelligence at a glance")
    if not company:
        st.info("Create or select a company in Company Setup to begin.")
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Profile attributes", session.query(ProfileAttribute).filter_by(company_id=company.id).count())
    completed = session.query(InterviewSession).filter_by(company_id=company.id, status="completed").count()
    c2.metric("Completed interviews", completed)
    c3.metric("Approved sources", session.query(Source).filter_by(company_id=company.id, status="approved").count())
    c4.metric("Collected documents", session.query(Document).filter_by(company_id=company.id).count())
    c5, c6, c7 = st.columns(3)
    c5.metric("Evidence-linked signals", session.query(Signal).filter_by(company_id=company.id).count())
    latest_brief = session.scalar(select(Brief).where(Brief.company_id == company.id).order_by(Brief.created_at.desc()))
    c6.metric("Latest brief", _fmt_datetime(latest_brief.created_at) if latest_brief else "None yet")
    latest_crawl = session.scalar(select(CrawlRun).where(CrawlRun.company_id == company.id).order_by(CrawlRun.started_at.desc()))
    c7.metric("Latest source run", _fmt_datetime(latest_crawl.started_at) if latest_crawl else "None yet")
    schedule_rows = session.scalars(select(AutomationSchedule).where(AutomationSchedule.company_id == company.id)).all()
    st.markdown("#### Scheduled work")
    if schedule_rows:
        st.dataframe(pd.DataFrame([{"Job": x.job_name, "Enabled": x.enabled, "Next run (UTC)": _fmt_datetime(x.next_run_at), "Interval (days)": x.interval_days} for x in schedule_rows]), width="stretch", hide_index=True)
        st.caption("The scheduler runs only while its separate local process is open. It is not active in a sleeping or stopped host.")
    else:
        st.info("Schedules are created when you save the company settings.")
    failed = session.scalars(select(ScheduledJobRun).where(ScheduledJobRun.company_id == company.id, ScheduledJobRun.status.in_(["failed", "partial"])).order_by(ScheduledJobRun.started_at.desc()).limit(5)).all()
    if failed:
        st.warning("Recent scheduled run failures or partial runs")
        for row in failed:
            st.text(f"{row.job_name} — {_fmt_datetime(row.started_at)} — {row.status}: {row.error or row.summary}")
    st.markdown("#### Recent ranked signals")
    signals = recent_signals(session, company.id, days=90, limit=5)
    if not signals:
        st.info("No evidence-backed signals have been collected yet. Candidate sources are not counted as intelligence.")
    for signal in signals:
        dev = session.get(Development, signal.development_id)
        st.text(f"{signal.score:.2f} · {signal.category.title()} · {dev.signal_type.title()} — {dev.what_happened}")


def _page_company_setup(session, company):
    st.subheader("Create a company configuration")
    with st.form("new_company"):
        name = st.text_input("Company name")
        website = st.text_input("Company website", placeholder="https://example.com")
        timezone_name = st.text_input("Time zone", value=settings.default_timezone)
        submitted = st.form_submit_button("Create company")
    if submitted:
        try:
            created = create_company(session, name, website, timezone=timezone_name.strip() or "Asia/Kolkata")
            discover_sources(session, created.id)
            ensure_schedules(session, created)
            session.commit()
            st.success("Company created. Initial candidates need review and approval before collection.")
            st.text(created.name)
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error("Could not create company:")
            st.text(str(exc))
    st.divider()
    st.markdown("#### Delete a company")
    st.warning("This permanently deletes the selected company's profile, sources, documents, signals, briefs, schedules, history, and uploaded files.")
    all_companies = session.scalars(select(Company).order_by(Company.name, Company.website)).all()
    company_choices = {row.id: row for row in all_companies}
    delete_id = st.selectbox(
        "Company to delete", list(company_choices),
        format_func=lambda company_id: f"{company_choices[company_id].name} — {company_choices[company_id].website}",
        key="delete_company_id", disabled=not company_choices,
    )
    delete_target = company_choices.get(delete_id)
    typed_name = st.text_input("Type the exact company name to confirm", key="delete_company_confirmation")
    can_delete = bool(delete_target and typed_name == delete_target.name)
    if st.button("Delete company and its data", disabled=not can_delete, key="delete_company_confirm") and delete_target:
        try:
            result = delete_company(session, delete_target.id)
            st.success(f"Deleted company and {result['files_removed']} uploaded file(s).")
            st.text(result["company_name"])
            if result["file_errors"]:
                st.warning(f"{result['file_errors']} file(s) were outside the managed upload directory or could not be removed; database records were deleted.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error("Could not delete company:")
            st.text(str(exc))
    start_onboarding = st.button("Run / resume onboarding", disabled=not bool(company), key="run_company_onboarding")
    if start_onboarding and company:
        stages = run_onboarding(session, company.id)
        failures = [f"{name}: {item.error}" for name, item in stages.items() if item.status == "failed"]
        if failures:
            st.warning("Onboarding is partially complete. You can retry failed stages.")
            st.text("\n".join(failures))
        else:
            st.success("Onboarding stages are complete. Review profile, source, competitor, and interview evidence below.")
    if company:
        st.markdown("#### Selected company")
        st.text(f"{company.name} · {company.website}")
        onboarding_rows = session.scalars(select(OnboardingStage).where(OnboardingStage.company_id == company.id).order_by(OnboardingStage.id)).all()
        if onboarding_rows:
            st.markdown("#### Onboarding progress")
            st.dataframe(pd.DataFrame([{"Stage": row.stage.replace("_", " ").title(), "Status": row.status, "Attempts": row.attempts, "Last attempt": _fmt_datetime(row.completed_at), "Error": row.error or ""} for row in onboarding_rows]), width="stretch", hide_index=True)
        left, right = st.columns(2)
        if left.button("Research public homepage", key="research_company"):
            with st.spinner("Checking the public homepage and robots.txt…"):
                try:
                    observations = research_company(session, company)
                    discover_sources(session, company.id)
                    st.success("Homepage statements were extracted and stored with their source URL.")
                    st.json(observations)
                except Exception as exc:
                    st.error("Research could not complete:")
                    st.text(str(exc))
                    st.caption("This run is recorded in crawl history. Live network access is required; no facts were substituted.")
        if right.button("Generate source candidates", key="discover_company_sources"):
            sources = discover_sources(session, company.id)
            st.success(f"{len(sources)} context-based candidates are available for review. None were auto-approved.")
        st.markdown("#### Existing company configurations")
        rows = session.scalars(select(Company).order_by(Company.name)).all()
        st.dataframe(pd.DataFrame([{"Company": x.name, "Website": x.website, "Time zone": x.timezone, "Active": x.active} for x in rows]), width="stretch", hide_index=True)


def _page_profile(session, company):
    st.subheader("Company intelligence profile")
    if not company:
        st.info("Select a company first.")
        return
    attrs = profile_values(session, company.id)
    st.markdown("#### Broader public research")
    st.caption("This optional search sends the company name and source-stated profile context to the configured search provider. Founder interview answers and uploaded files are excluded. Search snippets remain unverified discovery metadata; original pages must be reviewed and crawled before use as factual evidence.")
    provider_error = None
    try:
        research_provider = configured_search_provider()
    except ValueError as exc:
        research_provider = None
        provider_error = str(exc)
    if provider_error:
        st.error("Public research provider configuration is invalid:")
        st.text(provider_error)
    elif research_provider is None:
        st.info("No public search provider is configured. Set SIGNAL_STACK_RESEARCH_SEARCH_PROVIDER=tavily and SIGNAL_STACK_TAVILY_API_KEY to enable this step.")
    if st.button("Search for public research sources", disabled=research_provider is None, key="search_public_research") and research_provider:
        with st.spinner("Searching public company, customer, competitor, and market sources…"):
            discovery = discover_public_research_sources(session, company.id, provider=research_provider)
        st.success(f"Added {len(discovery.sources)} unapproved source candidate(s) from {discovery.completed_queries} completed searches.")
        for error in discovery.errors:
            st.text(error)
        st.info("Search result snippets are not company facts. Validate each original URL, inspect the page, and approve a source only after the normal access and relevance review.")
    st.markdown("#### Evidence-based industry and business classification")
    if st.button("Reassess industry and business model", key="reassess_classification"):
        classify_company_context(session, company.id)
        st.rerun()
    classification = session.scalar(select(CompanyClassification).where(CompanyClassification.company_id == company.id).order_by(CompanyClassification.created_at.desc(), CompanyClassification.id.desc()))
    if classification:
        st.json({
            "Industry": classification.industry or "Unknown",
            "Sub-industry": classification.sub_industry or "Unknown",
            "Business model": classification.business_model or "Unknown",
            "Products / services": json.loads(classification.products_services_json),
            "Customer segments": json.loads(classification.customer_segments_json),
            "Geography": json.loads(classification.geography_json),
            "Customer problems": json.loads(classification.customer_problems_json),
            "Business drivers": json.loads(classification.business_drivers_json),
            "Regulatory environment": json.loads(classification.regulatory_environment_json),
        })
        st.text(f"Provider: {classification.provider}; classification confidence {classification.confidence:.2f} (not fact verification); uncertainty: {', '.join(json.loads(classification.uncertainty)) or 'none recorded'}.")
        evidence = json.loads(classification.evidence_json)
        if evidence:
            st.dataframe(pd.DataFrame(evidence), width="stretch", hide_index=True)
    else:
        st.info("No classification snapshot exists yet. Run onboarding or reassess after adding evidenced context.")
    st.markdown("#### Source-stated and founder-provided context")
    if attrs:
        rows = session.scalars(select(ProfileAttribute).where(ProfileAttribute.company_id == company.id).order_by(ProfileAttribute.key)).all()
        st.dataframe(pd.DataFrame([{"Field": x.key, "Value": x.value, "Status": x.status, "Evidence URL": x.evidence_url or "—", "Evidence snippet": x.evidence_snippet or "—"} for x in rows]), width="stretch", hide_index=True)
    else:
        st.info("No company research or interview answers are stored yet.")
    field_specs = [
        ("industry", "Industry / problem space"), ("customer_segments", "Priority customer segments"),
        ("customer_problem", "Problems customers have"), ("competitors", "Known alternatives / substitutes"),
        ("geography", "Priority geography"), ("growth_priority", "Growth priority (6–12 months)"),
    ]
    with st.form("profile_edit"):
        entered = {key: st.text_input(label, value=attrs.get(key, ""), key=f"profile_{key}") for key, label in field_specs}
        reason = st.text_input("Why is the context changing?", value="Updated founder context")
        save = st.form_submit_button("Save context and reassess sources")
    if save:
        for key, value in entered.items():
            existing = session.scalar(select(ProfileAttribute).where(ProfileAttribute.company_id == company.id, ProfileAttribute.key == key))
            if value.strip():
                save_profile_value(session, company.id, key, value, status="founder-provided")
            elif existing:
                session.delete(existing)
        audits = reassess_sources(session, company.id, reason.strip() or "company profile updated")
        st.success(f"Saved a new profile version and created {len(audits)} source review recommendations. Existing approvals were not changed.")
        st.rerun()
    versions = session.execute(select(ProfileVersion).where(ProfileVersion.company_id == company.id).order_by(ProfileVersion.version.desc())).scalars().all()
    if versions:
        st.markdown("#### Profile history")
        st.dataframe(pd.DataFrame([{"Version": x.version, "Created": _fmt_datetime(x.created_at), "Reason": x.reason} for x in versions]), width="stretch", hide_index=True)
    audits = session.scalars(select(SourceAudit).where(SourceAudit.company_id == company.id).order_by(SourceAudit.created_at.desc()).limit(10)).all()
    if audits:
        st.markdown("#### Recent source reassessment notes")
        for audit in audits:
            st.text(f"{audit.status} — {audit.recommendation}")


def _page_interview(session, company):
    st.subheader("Adaptive founder interview")
    if not company:
        st.info("Select a company first.")
        return
    if st.button("Start or continue interview", key="start_interview"):
        start_interview(session, company.id)
        st.rerun()
    interview = session.scalar(select(InterviewSession).where(InterviewSession.company_id == company.id).order_by(InterviewSession.id.desc()))
    if not interview:
        st.info("The company-specific interview is generated after public research and profile context are available.")
        return
    questions = interview.questions
    answered = sum(q.answered_at is not None for q in questions)
    st.progress(answered / max(1, len(questions)), text=f"{answered} of {len(questions)} core questions completed or skipped")
    st.caption("One question is shown at a time. The system stores answers as founder-provided context; use Skip for confidential questions.")
    st.text(f"Question generation: {interview.generation_method}.")
    if interview.generation_note:
        st.warning("Local LLM was unavailable or returned invalid data; deterministic profile questions were used. Detail:")
        st.text(interview.generation_note)
    if interview.status == "completed":
        st.success("Ten-question interview completed. Answers are available in the profile and can be edited there.")
    with st.expander("Review or edit saved answers"):
        for saved_question in questions:
            st.text(f"{saved_question.ordinal}. {saved_question.question}")
            with st.form(f"edit_answer_{saved_question.id}"):
                edited = st.text_area("Founder-provided answer", value=saved_question.answer or "", key=f"edit_answer_text_{saved_question.id}", height=90)
                save_edit = st.form_submit_button("Save answer update")
            if save_edit:
                saved_question.answer = edited.strip() or None
                saved_question.skipped = not bool(edited.strip())
                saved_question.answered_at = utcnow()
                if edited.strip():
                    save_profile_value(session, company.id, f"interview_{saved_question.topic}", edited, status="founder-provided")
                else:
                    existing = session.scalar(select(ProfileAttribute).where(ProfileAttribute.company_id == company.id, ProfileAttribute.key == f"interview_{saved_question.topic}"))
                    if existing:
                        session.delete(existing)
                save_profile_version(session, company.id, f"interview answer updated: {saved_question.topic}")
                session.commit()
                st.success("Saved answer update.")
                st.rerun()
    if interview.status == "completed":
        return
    question = current_question(interview)
    if not question:
        st.info("No pending question.")
        return
    st.text(f"Question {question.ordinal} of 10 · {question.topic.replace('_', ' ').title()}")
    st.text(question.question)
    follow = bool(question.follow_up and question.answer and not question.answered_at)
    if follow:
        st.text(question.follow_up)
    with st.form(f"answer_{question.id}_{'follow' if follow else 'main'}"):
        answer = st.text_area("Your answer", value="" if follow else (question.answer or ""), height=150)
        col1, col2 = st.columns(2)
        submitted = col1.form_submit_button("Save follow-up answer" if follow else "Save answer")
        skipped = col2.form_submit_button("Skip confidential question")
    if submitted or skipped:
        try:
            followup = submit_answer(session, question.id, answer, skip=skipped, follow_up_answer=follow and submitted)
            if followup:
                st.text(followup)
            st.rerun()
        except Exception as exc:
            st.error("The answer could not be saved:")
            st.text(str(exc))


def _page_competitors(session, company):
    st.subheader("Competitors and substitutes")
    discover = st.button("Discover candidates from collected evidence", disabled=not bool(company), key="discover_competitors")
    if discover and company:
        candidates = discover_competitors_from_documents(session, company.id)
        st.success(f"Added {len(candidates)} cited competitor candidates for review; none were approved automatically.")
    if not company:
        st.info("Select a company first.")
        return
    st.caption("Names entered here are founder-provided candidates. Publicly discovered names must be supported by evidence before approval.")
    with st.form("add_competitor"):
        name = st.text_input("Competitor or substitute")
        rationale = st.text_area("Why buyers compare it with this company")
        evidence_url = st.text_input("Evidence URL (optional)")
        submitted = st.form_submit_button("Add candidate")
    if submitted:
        try:
            add_competitor(session, company.id, name, rationale, evidence_url.strip() or None)
            st.success("Candidate saved.")
            st.rerun()
        except Exception as exc:
            st.error("The competitor candidate could not be saved:")
            st.text(str(exc))
    competitors = session.scalars(select(Competitor).where(Competitor.company_id == company.id).order_by(Competitor.discovery_confidence.desc(), Competitor.name)).all()
    if not competitors:
        st.info("No competitors have been evidenced or provided yet; the system will not invent a list.")
    else:
        st.dataframe(pd.DataFrame([{"Name": x.name, "Type": x.competitor_type, "Status": x.status, "Confidence": x.discovery_confidence, "Products / services": x.products_services or "Unknown", "Target market": x.target_market or "Unknown", "Customer segments": x.customer_segments or "Unknown", "Geography": x.geographic_focus or "Unknown", "Relationship": x.competitive_relationship or "Unassessed", "Discovered": _fmt_datetime(x.discovered_at), "Supporting sources": x.supporting_source_urls_json or "[]", "Reason": x.rationale, "Evidence": x.evidence_url or "—", "Evidence excerpt": x.evidence_snippet or "—"} for x in competitors]), width="stretch", hide_index=True)
        ids = {x.id: x for x in competitors}
        selected_id = st.selectbox("Review candidate", list(ids), format_func=lambda x: ids[x].name, key="competitor_review")
        selected = ids[selected_id]
        types = ["unclassified", "direct", "indirect", "substitute", "emerging"]
        statuses = ["candidate", "approved", "rejected"]
        with st.form("edit_competitor"):
            edit_name = st.text_input("Name", value=selected.name)
            edit_website = st.text_input("Website (leave blank if unsupported)", value=selected.website or "")
            edit_type = st.selectbox("Competitor type", types, index=types.index(selected.competitor_type) if selected.competitor_type in types else 0)
            edit_rationale = st.text_area("Rationale", value=selected.rationale)
            edit_products = st.text_area("Products / services (unknown unless supported)", value=selected.products_services)
            edit_market = st.text_area("Target market (unknown unless supported)", value=selected.target_market)
            edit_segments = st.text_area("Customer segments (unknown unless supported)", value=selected.customer_segments)
            edit_geography = st.text_area("Geographic focus (unknown unless supported)", value=selected.geographic_focus)
            edit_relationship = st.text_area("Competitive relationship (cite the basis)", value=selected.competitive_relationship)
            try:
                saved_supporting_urls = json.loads(selected.supporting_source_urls_json or "[]")
            except json.JSONDecodeError:
                saved_supporting_urls = []
            if not isinstance(saved_supporting_urls, list):
                saved_supporting_urls = []
            saved_supporting_urls = [str(url) for url in saved_supporting_urls if isinstance(url, str)]
            edit_supporting_urls = st.text_area("Supporting source URLs (one per line)", value="\n".join(saved_supporting_urls))
            st.caption("Leave unsupported profile fields blank. Editing these details does not independently verify them.")
            edit_status = st.selectbox("Review status", statuses, index=statuses.index(selected.status) if selected.status in statuses else 0)
            save_edit = st.form_submit_button("Save competitor edits")
        if save_edit:
            try:
                updated = update_competitor(
                    session, company.id, selected.id, name=edit_name, website=edit_website or None,
                    competitor_type=edit_type, rationale=edit_rationale,
                    profile_fields={
                        "products_services": edit_products, "target_market": edit_market,
                        "customer_segments": edit_segments, "geographic_focus": edit_geography,
                        "competitive_relationship": edit_relationship,
                    },
                    supporting_source_urls=[line.strip() for line in edit_supporting_urls.splitlines() if line.strip()],
                )
                updated.status = edit_status
                session.commit()
                st.success("Competitor details saved; original evidence provenance was preserved.")
                st.rerun()
            except Exception as exc:
                st.error("Competitor details could not be saved:")
                st.text(str(exc))


def _page_signal_stack(session, company):
    st.subheader("Signal Stack")
    if not company:
        st.info("Select a company first.")
        return
    categories = ["customers", "competitors", "market", "internal"]
    tabs = st.tabs([x.title() for x in categories])
    for tab, category in zip(tabs, categories):
        with tab:
            sources = session.scalars(select(Source).where(Source.company_id == company.id, Source.category == category).order_by(Source.status, Source.title)).all()
            if not sources:
                st.info("No source is configured for this category yet.")
            for source in sources:
                st.text(f"{source.title} · {source.status} · relevance {source.relevance:.2f} · reliability {source.reliability:.2f}")
                st.text(source.reason)
                st.text(source.url)


def _page_sources(session, company):
    st.subheader("Source manager")
    if not company:
        st.info("Select a company first.")
        return
    with st.form("manual_source"):
        url = st.text_input("Public HTTP(S) source URL")
        title = st.text_input("Source title")
        category = st.selectbox("Category", ["customers", "competitors", "market", "internal"])
        source_type = st.selectbox("Type", ["website", "rss", "search_feed"])
        reason = st.text_area("Reason for monitoring")
        add = st.form_submit_button("Add candidate source")
    if add:
        try:
            normalized = normalize_url(url)
            upsert_source(session, company.id, url=normalized, title=title or normalized, category=category, source_type=source_type, reason=reason or "Manually added; justify relevance before approval.", reliability=.4)
            session.commit()
            st.success("Added as an unapproved candidate.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error("The source candidate could not be added:")
            st.text(str(exc))
    sources = session.scalars(select(Source).where(Source.company_id == company.id).order_by(Source.category, Source.title)).all()
    if not sources:
        st.info("No candidates yet. Generate candidates from Company Setup.")
        return
    st.dataframe(pd.DataFrame([{"ID": x.id, "Category": x.category, "Title": x.title, "Type": x.source_type, "Status": x.status, "Relevance": x.relevance, "Relevance basis": x.relevance_reason, "Reliability": x.reliability, "Health": x.last_status or "unchecked", "Last attempted": _fmt_datetime(x.last_attempted_at), "Last successful": _fmt_datetime(x.last_success_at), "Next eligible": _fmt_datetime(x.next_due_at), "Reason": x.reason, "URL": x.url} for x in sources]), width="stretch", hide_index=True)
    by_id = {x.id: x for x in sources}
    approval_cutoff = utcnow() - timedelta(days=7)
    batch_candidates = [source for source in sources if source.status == "candidate" and source.source_type != "internal" and source.last_status == "healthy" and source.last_checked_at and source.last_checked_at >= approval_cutoff]
    with st.form("bulk_source_approval"):
        st.caption("Review candidate evidence above. Multiple candidates can be approved together after each passes a public access and health check within seven days.")
        selected_batch = st.multiselect("Validated candidates to approve", [source.id for source in batch_candidates], format_func=lambda sid: f"{by_id[sid].title} (#{sid})")
        approve_batch = st.form_submit_button("Approve selected validated sources", disabled=not selected_batch)
    if approve_batch:
        try:
            approved = approve_validated_sources(session, company.id, selected_batch)
            st.success(f"Approved {len(approved)} reviewed source(s).")
            st.rerun()
        except Exception as exc:
            st.error("The source review could not be saved:")
            st.text(str(exc))
    with st.form("source_review"):
        source_id = st.selectbox("Select source", list(by_id), format_func=lambda sid: f"{by_id[sid].title} (#{sid})")
        action = st.selectbox("Source action", ["approved", "candidate", "rejected", "paused", "removed"])
        reviewer = st.text_input("Reviewer identity", value=str(session.info.get("principal", "")))
        save = st.form_submit_button("Save source status")
    if save:
        try:
            set_source_status(session, company.id, source_id, action, reviewer_identity=reviewer)
            st.rerun()
        except Exception as exc:
            st.error("The source status could not be saved:")
            st.text(str(exc))
    cols = st.columns(2)
    if cols[0].button("Validate selected source"):
        source, audit = validate_source(session, company.id, source_id)
        if audit.status == "candidate-healthy" or audit.status == "internal-only":
            st.success(f"Validation status: {audit.status}. Review the result before approval.")
        else:
            st.error("Source validation needs review:")
            st.text(audit.recommendation)
        st.rerun()
    if cols[1].button("Crawl selected approved source"):
        try:
            result = crawl_source(session, company.id, source_id)
            st.success(f"Crawl run #{result.id}: {result.status}, {result.documents_found} new document(s).")
            if result.error:
                st.warning("The crawl reported an issue:")
                st.text(result.error)
        except Exception as exc:
            st.error("The crawl could not complete:")
            st.text(str(exc))
    if cols[1].button("Generate context-based candidates"):
        st.success(f"Generated {len(discover_sources(session, company.id))} candidate sources. Review them before approval.")
        st.rerun()
    runs = session.scalars(select(CrawlRun).where(CrawlRun.company_id == company.id).order_by(CrawlRun.started_at.desc()).limit(30)).all()
    if runs:
        st.markdown("#### Crawl history and failures")
        st.dataframe(pd.DataFrame([{"Run": x.id, "Source ID": x.source_id, "Started": _fmt_datetime(x.started_at), "Status": x.status, "New documents": x.documents_found, "Error": x.error or "—"} for x in runs]), width="stretch", hide_index=True)
    duplicates = session.execute(select(Document.content_hash, func.count(Document.id)).where(Document.company_id == company.id).group_by(Document.content_hash).having(func.count(Document.id) > 1)).all()
    st.caption(f"Duplicate content hashes present across different URLs: {len(duplicates)}. Exact URL and content repeats are suppressed during collection.")


def _page_feed(session, company):
    st.subheader("Intelligence feed")
    if not company:
        st.info("Select a company first.")
        return
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    days = col1.selectbox("Discovered in the last", [7, 30, 90, 365], index=1)
    category = col2.selectbox("Category", ["all", "customers", "competitors", "market", "internal"])
    kind = col3.selectbox("Signal type", ["all", "opportunity", "threat", "neutral"])
    verification = col4.selectbox("Verification", ["all", "not-reviewed", "source-reviewed", "independently-corroborated", "disputed"])
    competitors = session.scalars(select(Competitor).where(Competitor.company_id == company.id).order_by(Competitor.name)).all()
    competitor_choice = col5.selectbox("Competitor", ["all"] + [x.name for x in competitors])
    order = col6.selectbox("Order", ["newest", "score", "oldest"], index=0, format_func=lambda value: {"newest": "Newest first", "score": "Highest score", "oldest": "Oldest first"}[value])
    relevance_min = st.slider("Minimum strategic relevance", 0.0, 1.0, 0.0, 0.05)
    impact_min = st.slider("Minimum business impact", 0.0, 1.0, 0.0, 0.05)
    urgency_min = st.slider("Minimum urgency", 0.0, 1.0, 0.0, 0.05)
    signals = recent_signals(session, company.id, days=days, category=category, limit=500, order=order)
    filtered = []
    for signal in signals:
        dev = session.get(Development, signal.development_id)
        doc = session.get(Document, dev.document_id)
        if kind != "all" and dev.signal_type != kind:
            continue
        if verification != "all" and dev.verification_status != verification:
            continue
        if signal.relevance < relevance_min or signal.impact < impact_min or signal.urgency < urgency_min:
            continue
        if competitor_choice != "all" and competitor_choice.casefold() not in f"{doc.title} {doc.content}".casefold():
            continue
        filtered.append(signal)
    signals = filtered
    if not signals:
        st.info("No signal matches these filters. Candidate sources and research baselines are not intelligence signals.")
        return
    for signal in signals:
        dev = session.get(Development, signal.development_id)
        doc = session.get(Document, dev.document_id)
        source = session.get(Source, doc.source_id) if doc.source_id else None
        with st.expander(f"Signal #{signal.id} · {signal.score:.2f} · {signal.category.title()} · {dev.signal_type.title()}"):
            st.text(dev.what_happened)
            st.text(f"Publication: {_fmt_datetime(doc.published_at)} · Event: {_fmt_datetime(doc.event_date)} · Discovered: {_fmt_datetime(doc.discovered_at)} · Modified: {_fmt_datetime(doc.modified_at)} · Verification: {'verified' if dev.verified else 'not independently verified'}")
            st.text(f"Source claim: {dev.factual_claim or 'Not assessed in this legacy record.'}")
            st.text(f"Interpretation: {dev.interpretation or dev.business_implication}")
            st.text(f"Affected area: {dev.affected_business_area or 'Unknown'}")
            st.text(f"Recommended action: {dev.recommended_action or 'No action recorded.'}")
            st.text(f"Analysis confidence {dev.analysis_confidence:.2f} ({dev.analysis_method}); signal classifier {dev.signal_classification_method}; {dev.confidence_rationale or 'No rationale stored.'}")
            st.text("Evidence:")
            st.text(doc.content[:1200])
            st.text(f"Document: {doc.title}\nURL: {doc.canonical_url}")
            st.text(f"Verification status: {dev.verification_status}. Independent corroboration requires a distinct source or domain.")
            available_transitions = {
                "not-reviewed": ["source-reviewed", "disputed"],
                "source-reviewed": ["independently-corroborated", "disputed"],
                "independently-corroborated": ["disputed"],
                "disputed": ["source-reviewed", "independently-corroborated"],
            }.get(dev.verification_status, [])
            with st.form(f"verification_{signal.id}"):
                requested_status = st.selectbox("New verification state", available_transitions, key=f"verification_state_{signal.id}") if available_transitions else None
                reviewer = st.text_input("Reviewer identity", value="", key=f"verification_reviewer_{signal.id}")
                explanation = st.text_area("Review explanation", key=f"verification_explanation_{signal.id}")
                evidence_urls = st.text_input("Independent evidence URL(s), comma separated", key=f"verification_urls_{signal.id}")
                submitted = st.form_submit_button("Save verification review", disabled=not available_transitions)
            if submitted and requested_status:
                try:
                    transition_verification(
                        session, company.id, dev.id, requested_status,
                        reviewer_identity=reviewer,
                        explanation=explanation,
                        evidence_urls=[item.strip() for item in evidence_urls.split(",") if item.strip()],
                    )
                    session.commit()
                    st.success("Verification history saved.")
                    st.rerun()
                except ValueError as exc:
                    session.rollback()
                    st.error("Verification review could not be saved:")
                    st.text(str(exc))
            history = verification_history(session, company.id, dev.id)
            if history:
                with st.expander("Verification history"):
                    for item in history:
                        st.text(f"{item.previous_status} → {item.new_status} · {item.reviewer_identity} · {_fmt_datetime(item.created_at)}")
                        st.text(item.explanation)
                        if item.evidence_urls_json != "[]":
                            st.text(f"Evidence URLs: {item.evidence_urls_json}")
            st.text(f"Source: {source.title if source else 'Internal file'} · reliability {signal.source_reliability:.2f}")
            st.json({"relevance": signal.relevance, "impact": signal.impact, "urgency": signal.urgency, "source_reliability": signal.source_reliability, "evidence_strength": signal.evidence_strength, "recency": signal.recency, "novelty": signal.novelty, "overall": signal.score})
            st.text(signal.score_reason)


def _page_brief(session, company):
    st.subheader("Monday Morning Brief")
    if not company:
        st.info("Select a company first.")
        return
    openrouter_ready = (
        settings.llm_provider == "openrouter"
        and bool(settings.openrouter_api_key)
        and settings.openrouter_brief_model.strip().endswith(":free")
    )
    with st.form(f"generate_brief_{company.id}"):
        period_days = st.selectbox("Reporting window (days)", [7, 14, 30], index=0)
        use_llm = st.checkbox(
            "Add OpenRouter synthesis from public evidence",
            value=False,
            disabled=not openrouter_ready,
            key=f"use_llm_brief_synthesis_{company.id}",
            help=(
                "When enabled, the configured model receives the company name, public signal excerpts, and only source-stated company profile fields. "
                "Internal uploads, founder interview answers, and founder-provided profile fields stay local. "
                "Generated implications are suggestions; each summary must include a quote matched to the collected source text."
            ),
        )
        generate = st.form_submit_button("Generate and save brief", type="primary")
    if not openrouter_ready:
        st.caption("OpenRouter synthesis is unavailable. Configure SIGNAL_STACK_LLM_PROVIDER=openrouter and OPENROUTER_API_KEY to enable the opt-in; deterministic briefs remain available.")
    if generate:
        brief = generate_brief(session, company.id, days=period_days, use_llm=use_llm)
        st.success(f"Saved brief #{brief.id} using {brief.signal_count} stored signals.")
        st.rerun()
    brief = session.scalar(select(Brief).where(Brief.company_id == company.id).order_by(Brief.created_at.desc()))
    if not brief:
        st.info("No brief has been generated yet. The generator uses stored, evidence-linked signals only.")
        return
    st.caption(f"Brief #{brief.id} · {_fmt_datetime(brief.created_at)} · {brief.signal_count} signals in its reporting window")
    safe_markdown = sanitize_persisted_brief(brief.markdown)
    st.download_button("Download Markdown", safe_markdown, file_name=f"{company.name.lower().replace(' ', '-')}-brief-{brief.id}.md", mime="text/markdown")
    st.download_button("Download PDF", export_pdf(safe_markdown), file_name=f"{company.name.lower().replace(' ', '-')}-brief-{brief.id}.pdf", mime="application/pdf")
    st.markdown(safe_markdown)
    actions = session.scalars(select(BriefAction).where(BriefAction.brief_id == brief.id).order_by(BriefAction.id)).all()
    if actions:
        st.markdown("#### Track actions")
        for action in actions:
            col1, col2 = st.columns([4, 1])
            col1.text(f"{action.priority.title()} · {action.owner} — {action.action}")
            value = col2.selectbox("Status", ["not started", "in progress", "completed", "dismissed"], index=["not started", "in progress", "completed", "dismissed"].index(action.status), key=f"action_{action.id}")
            if value != action.status:
                action.status = value
                session.commit()
                st.rerun()


def _page_history(session, company):
    st.subheader("Brief history")
    if not company:
        st.info("Select a company first.")
        return
    briefs = brief_history(session, company.id)
    if not briefs:
        st.info("No saved briefs yet.")
        return
    selected = st.selectbox("Open saved brief", [b.id for b in briefs], format_func=lambda bid: f"#{bid} · {_fmt_datetime(next(x.created_at for x in briefs if x.id == bid))} · {next(x.signal_count for x in briefs if x.id == bid)} signals")
    brief = next(x for x in briefs if x.id == selected)
    safe_markdown = sanitize_persisted_brief(brief.markdown)
    st.download_button("Download Markdown", safe_markdown, file_name=f"brief-{brief.id}.md", mime="text/markdown")
    st.download_button("Download PDF", export_pdf(safe_markdown), file_name=f"brief-{brief.id}.pdf", mime="application/pdf")
    st.markdown(safe_markdown)


def _page_upload(session, company):
    st.subheader("Internal intelligence uploads")
    if not company:
        st.info("Select a company first.")
        return
    st.caption("Files are stored locally and parsed as untrusted evidence. They are not sent to an external LLM.")
    file = st.file_uploader("Upload CSV, XLSX, PDF, or TXT (10 MB maximum)", type=[x[1:] for x in ALLOWED_EXTENSIONS])
    mapping = {}
    if file and Path(file.name).suffix.casefold() in {".csv", ".xlsx"}:
        try:
            headers, _preview_rows = read_business_table(file.name, file.getvalue())
            with st.expander("Optional business-record column mapping"):
                st.caption("Exact common header names are detected automatically. Choose a column only when you need to override the mapping.")
                for semantic in sorted(SEMANTICS):
                    selected = st.selectbox(f"{semantic.replace('_', ' ').title()} column", ["(auto-detect)"] + headers, key=f"internal_map_{semantic}")
                    if selected != "(auto-detect)":
                        mapping[semantic] = selected
        except ValueError as exc:
            st.error("Could not inspect business-record columns:")
            st.text(str(exc))
    if file and st.button("Process internal evidence"):
        try:
            record, count, signal_count = ingest_upload(session, company.id, file.name, file.getvalue(), column_mapping=mapping)
            st.success(f"Stored {record.record_count} unique business record(s), {record.invalid_record_count} malformed row(s), {count} new document version(s), {signal_count} evidence-backed internal signal(s). Small samples do not establish trends.")
            st.text(record.filename)
        except Exception as exc:
            st.error("Upload processing failed:")
            st.text(str(exc))
    uploads = session.scalars(select(UploadedFile).where(UploadedFile.company_id == company.id).order_by(UploadedFile.uploaded_at.desc())).all()
    if uploads:
        st.dataframe(pd.DataFrame([{"Filename": x.filename, "Uploaded": _fmt_datetime(x.uploaded_at), "Status": x.status, "Error": x.error or "—"} for x in uploads]), width="stretch", hide_index=True)
        for upload in uploads:
            try:
                analysis = json.loads(upload.analysis_summary_json or "{}")
            except (TypeError, ValueError):
                analysis = {}
            with st.expander(f"Analysis coverage — upload #{upload.id}"):
                st.text(upload.filename)
                st.text(f"{analysis.get('unique_record_count', upload.record_count)} unique records · period {analysis.get('period_start') or 'unknown'} to {analysis.get('period_end') or 'unknown'} · evidence quality {analysis.get('evidence_quality', 'not assessed')}")
                for insight in analysis.get("insights", []):
                    st.text(f"{insight.get('statement', 'Observed internal record pattern')} · Evidence rows: {insight.get('evidence_rows', [])} · Confidence: {insight.get('confidence', 'unknown')}")
                for limitation in analysis.get("limitations", []):
                    st.text(str(limitation))


def _page_automation(session, company):
    st.subheader("Automation and job history")
    if not company:
        st.info("Select a company first.")
        return
    st.caption("The scheduler is a separate process. Manual runs below work while the dashboard is open, but do not keep the schedule alive.")
    col1, col2 = st.columns(2)
    if col1.button("Run weekly intelligence workflow now", type="primary"):
        result = execute_job(company.id, WEEKLY_JOB)
        st.success(f"Job #{result.id}: {result.status}.")
        st.text(result.summary or result.error or "No summary recorded.")
    if col2.button("Run monthly source audit now"):
        result = execute_job(company.id, MONTHLY_JOB)
        st.success(f"Job #{result.id}: {result.status}.")
        st.text(result.summary or result.error or "No summary recorded.")
    schedules = session.scalars(select(AutomationSchedule).where(AutomationSchedule.company_id == company.id)).all()
    if schedules:
        st.dataframe(pd.DataFrame([{"Job": s.job_name, "Enabled": s.enabled, "Next run (UTC)": _fmt_datetime(s.next_run_at), "Last run (UTC)": _fmt_datetime(s.last_run_at)} for s in schedules]), width="stretch", hide_index=True)
    jobs = session.scalars(select(ScheduledJobRun).where(ScheduledJobRun.company_id == company.id).order_by(ScheduledJobRun.started_at.desc()).limit(100)).all()
    if jobs:
        st.markdown("#### Execution log")
        st.dataframe(pd.DataFrame([{"Job ID": x.id, "Name": x.job_name, "Started": _fmt_datetime(x.started_at), "Ended": _fmt_datetime(x.ended_at), "Status": x.status, "Documents": x.documents_processed, "Signals": x.signals_generated, "Summary": x.summary or "", "Error": x.error or ""} for x in jobs]), width="stretch", hide_index=True)
    audits = session.scalars(select(SourceAudit).where(SourceAudit.company_id == company.id).order_by(SourceAudit.created_at.desc()).limit(50)).all()
    if audits:
        st.markdown("#### Source health audit recommendations")
        for audit in audits:
            st.text(f"{audit.created_at:%Y-%m-%d} · {audit.status}: {audit.recommendation}")


def _page_settings(session, company):
    st.subheader("Company and monitoring settings")
    if not company:
        st.info("Select a company first.")
        return
    with st.form("company_identity_settings"):
        updated_name = st.text_input("Company name", value=company.name)
        updated_website = st.text_input("Company website", value=company.website)
        update_reason = st.text_input("Reason for identity/profile update", value="Company details updated")
        save_identity = st.form_submit_button("Save company details and reassess sources")
    if save_identity:
        try:
            normalized = normalize_url(updated_website)
            if not updated_name.strip():
                raise ValueError("Company name is required")
            changed = normalized != company.website or updated_name.strip() != company.name
            company.website = normalized
            company.name = " ".join(updated_name.split())
            save_profile_value(session, company.id, "company_name", company.name, status="founder-provided")
            save_profile_value(session, company.id, "website", company.website, status="user-provided")
            if changed:
                audits = reassess_sources(session, company.id, update_reason.strip() or "Company details updated")
                st.success(f"Saved a profile version and created {len(audits)} source review recommendations. Old approvals were not silently removed.")
            else:
                session.commit()
                st.success("No company identity changes were needed.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            st.error("Could not save company details:")
            st.text(str(exc))
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    with st.form("company_settings"):
        timezone_name = st.text_input("Time zone (IANA)", value=company.timezone)
        weekday = st.selectbox("Weekly brief day", list(range(7)), index=company.brief_weekday, format_func=lambda x: weekdays[x])
        hour = st.slider("Weekly brief / monthly audit hour (local)", min_value=0, max_value=23, value=company.brief_hour)
        refresh = st.number_input("Source refresh frequency (hours)", min_value=1, max_value=720, value=company.source_refresh_hours)
        weekly = st.checkbox("Enable weekly intelligence brief", value=company.weekly_enabled)
        monthly = st.checkbox("Enable monthly source health audit", value=company.monthly_audit_enabled)
        save = st.form_submit_button("Save settings")
    if save:
        try:
            ZoneInfo(timezone_name.strip())
            if weekday not in range(7) or not 0 <= hour <= 23:
                raise ValueError("Invalid weekday or hour")
            company.timezone = timezone_name.strip()
            company.brief_weekday = int(weekday)
            company.brief_hour = int(hour)
            company.source_refresh_hours = int(refresh)
            company.weekly_enabled = weekly
            company.monthly_audit_enabled = monthly
            ensure_schedules(session, company)
            session.commit()
            st.success("Settings saved. The scheduler must be running separately to execute jobs.")
            st.rerun()
        except (ZoneInfoNotFoundError, ValueError) as exc:
            st.error("Invalid setting:")
            st.text(str(exc))
    weights = load_ranking_weights(session, company.id)
    with st.expander("Explainable signal ranking weights"):
        st.caption("Weights are normalized to sum to 1. Each score dimension remains visible; these settings do not change evidence verification.")
        with st.form("ranking_weights"):
            entered_weights = {}
            left, right = st.columns(2)
            for index, key in enumerate(DEFAULT_RANKING_WEIGHTS):
                container = left if index % 2 == 0 else right
                entered_weights[key] = container.slider(key.replace("_", " ").title(), 0.0, 1.0, float(weights[key]), 0.05, key=f"rank_weight_{key}")
            save_weights = st.form_submit_button("Save ranking weights")
        if save_weights:
            try:
                normalized = save_ranking_weights(session, company.id, entered_weights)
                session.commit()
                st.success(f"Saved normalized weights (total 1.00): {normalized}")
                st.rerun()
            except ValueError as exc:
                st.error("Ranking weights could not be saved:")
                st.text(str(exc))
    st.markdown("#### LLM status")
    if settings.llm_provider == "openrouter":
        free_models_ready = settings.openrouter_model.strip().endswith(":free") and settings.openrouter_brief_model.strip().endswith(":free")
        status = "API key and free model IDs configured" if settings.openrouter_api_key and free_models_ready else "API key or free model configuration missing; deterministic fallback active"
        st.info(f"OpenRouter signal-decision model: {settings.openrouter_model}; brief text model: {settings.openrouter_brief_model} · {status}. Public-signal classification uses the decision endpoint; Monday brief synthesis is separately opt-in and sends public-source evidence. Internal uploads and founder interview answers stay local.")
    else:
        st.info("OpenRouter is disabled. Signal classification uses deterministic local rules.")
    if settings.research_search_provider == "tavily":
        research_status = "API key configured" if settings.tavily_api_key else "API key missing"
        st.info(f"Public research search: Tavily · {research_status}. Results are unverified source candidates and require review.")
    else:
        st.info("Public research search is disabled. Configure Tavily to discover additional source candidates.")


def main() -> None:
    st.set_page_config(page_title="Signal Stack", page_icon="📡", layout="wide")
    if not authenticate_streamlit(st):
        return
    init_db()
    session = SessionLocal()
    session.info["principal"] = st.session_state.get("principal", "local-development")
    try:
        st.title("📡 Signal Stack")
        st.caption("Company intelligence and executive briefing engine · evidence first")
        companies = session.scalars(select(Company).where(Company.active.is_(True)).order_by(Company.name)).all()
        with st.sidebar:
            st.header("Workspace")
            page = st.radio("Navigate", NAVIGATION, key="navigation")
            company_id = None
            if companies:
                options = [c.id for c in companies]
                if st.session_state.get("selected_company_id") not in options:
                    st.session_state["selected_company_id"] = options[0]
                company_id = st.selectbox("Company", options, format_func=lambda cid: next(c.name for c in companies if c.id == cid), key="selected_company_id")
            else:
                st.info("No company yet")
            st.divider()
            st.caption("Local database persistence is active")
            if st.button("Log out", key="logout"):
                logout_streamlit(st)
        company = session.get(Company, company_id) if company_id else None
        render = {
            "Home / Overview": _page_home,
            "Company Setup": _page_company_setup,
            "Company Intelligence Profile": _page_profile,
            "Adaptive AI Interview": _page_interview,
            "Competitors": _page_competitors,
            "Signal Stack": _page_signal_stack,
            "Source Manager": _page_sources,
            "Intelligence Feed": _page_feed,
            "Monday Morning Brief": _page_brief,
            "Brief History": _page_history,
            "Internal File Upload": _page_upload,
            "Automation / Job History": _page_automation,
            "Settings": _page_settings,
        }[page]
        render(session, company)
        session.commit()
    except Exception as exc:
        session.rollback()
        st.error("Page operation failed:")
        st.text(str(exc))
    finally:
        session.close()


