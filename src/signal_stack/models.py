from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from signal_stack.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    website: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Kolkata", nullable=False)
    brief_weekday: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    brief_hour: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    weekly_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    monthly_audit_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_refresh_hours: Mapped[int] = mapped_column(Integer, default=24, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    profile_attributes: Mapped[list["ProfileAttribute"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    sources: Mapped[list["Source"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    competitors: Mapped[list["Competitor"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class OnboardingStage(Base):
    __tablename__ = "onboarding_stages"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("company_id", "stage"),)


class ProfileVersion(Base):
    __tablename__ = "company_profile_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), default="profile update", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("company_id", "version"),)


class CompanyClassification(Base):
    """Evidence-linked classification snapshots; each row preserves prior assessments."""
    __tablename__ = "company_classifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    industry: Mapped[str | None] = mapped_column(String(160))
    sub_industry: Mapped[str | None] = mapped_column(String(200))
    business_model: Mapped[str | None] = mapped_column(String(240))
    products_services_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    customer_segments_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    geography_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    customer_problems_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    business_drivers_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    regulatory_environment_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    uncertainty: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="deterministic-profile", nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ProfileAttribute(Base):
    __tablename__ = "company_profile_attributes"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="inferred", nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(2000))
    evidence_snippet: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    company: Mapped[Company] = relationship(back_populates="profile_attributes")
    __table_args__ = (UniqueConstraint("company_id", "key"),)


class InterviewSession(Base):
    __tablename__ = "interview_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    generation_method: Mapped[str] = mapped_column(String(48), default="deterministic-profile", nullable=False)
    generation_note: Mapped[str | None] = mapped_column(Text)
    context_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    questions: Mapped[list["InterviewQuestion"]] = relationship(back_populates="session", cascade="all, delete-orphan", order_by="InterviewQuestion.ordinal")


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("interview_sessions.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(String(100), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    follow_up: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)
    session: Mapped[InterviewSession] = relationship(back_populates="questions")
    __table_args__ = (UniqueConstraint("session_id", "ordinal"),)


class Competitor(Base):
    __tablename__ = "competitors"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    website: Mapped[str | None] = mapped_column(String(1000))
    competitor_type: Mapped[str] = mapped_column(String(32), default="unclassified", nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(2000))
    evidence_snippet: Mapped[str | None] = mapped_column(Text)
    discovery_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    products_services: Mapped[str] = mapped_column(Text, default="", nullable=False)
    target_market: Mapped[str] = mapped_column(Text, default="", nullable=False)
    customer_segments: Mapped[str] = mapped_column(Text, default="", nullable=False)
    geographic_focus: Mapped[str] = mapped_column(Text, default="", nullable=False)
    competitive_relationship: Mapped[str] = mapped_column(Text, default="", nullable=False)
    supporting_source_urls_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    company: Mapped[Company] = relationship(back_populates="competitors")
    __table_args__ = (UniqueConstraint("company_id", "name"),)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), default="website", nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reliability: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    relevance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    relevance_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    refresh_claimed_until: Mapped[datetime | None] = mapped_column(DateTime)
    refresh_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_status: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    company: Mapped[Company] = relationship(back_populates="sources")
    __table_args__ = (UniqueConstraint("company_id", "url"), Index("ix_sources_company_status", "company_id", "status"))


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    documents_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    canonical_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    event_date: Mapped[datetime | None] = mapped_column(DateTime)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_article: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reliability: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("company_id", "canonical_url", "content_hash"),
        Index("ix_documents_company_discovered", "company_id", "discovered_at"),
    )


class Development(Base):
    __tablename__ = "developments"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    what_happened: Mapped[str] = mapped_column(Text, nullable=False)
    business_implication: Mapped[str] = mapped_column(Text, nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(40), default="not-reviewed", nullable=False)
    factual_claim: Mapped[str] = mapped_column(Text, default="", nullable=False)
    interpretation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    affected_business_area: Mapped[str] = mapped_column(String(240), default="unassessed", nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, default="", nullable=False)
    analysis_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confidence_rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    analysis_method: Mapped[str] = mapped_column(String(80), default="legacy-unassessed", nullable=False)
    signal_classification_method: Mapped[str] = mapped_column(String(80), default="legacy-unassessed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("company_id", "document_id", "what_happened"),)


class VerificationHistory(Base):
    __tablename__ = "verification_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    development_id: Mapped[int] = mapped_column(ForeignKey("developments.id", ondelete="CASCADE"), index=True)
    previous_status: Mapped[str] = mapped_column(String(40), nullable=False)
    new_status: Mapped[str] = mapped_column(String(40), nullable=False)
    reviewer_identity: Mapped[str] = mapped_column(String(240), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_urls_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    evidence_source_ids_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    development_id: Mapped[int] = mapped_column(ForeignKey("developments.id", ondelete="CASCADE"), unique=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    relevance: Mapped[float] = mapped_column(Float, nullable=False)
    impact: Mapped[float] = mapped_column(Float, nullable=False)
    urgency: Mapped[float] = mapped_column(Float, nullable=False)
    source_reliability: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_strength: Mapped[float] = mapped_column(Float, nullable=False)
    novelty: Mapped[float] = mapped_column(Float, nullable=False)
    recency: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    score_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    evidence: Mapped[list["SignalEvidence"]] = relationship(back_populates="signal", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_signals_company_score", "company_id", "score"),)


class SignalEvidence(Base):
    __tablename__ = "signal_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    evidence_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    signal: Mapped[Signal] = relationship(back_populates="evidence")
    __table_args__ = (UniqueConstraint("signal_id", "document_id"),)


class Brief(Base):
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    signal_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actions: Mapped[list["BriefAction"]] = relationship(back_populates="brief", cascade="all, delete-orphan")


class BriefAction(Base):
    __tablename__ = "brief_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    brief_id: Mapped[int] = mapped_column(ForeignKey("briefs.id", ondelete="CASCADE"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(24), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), default="Founder / leadership", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="not started", nullable=False)
    brief: Mapped[Brief] = relationship(back_populates="actions")


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="stored", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    analysis_summary_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_record_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ScheduledJobRun(Base):
    __tablename__ = "scheduled_job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    job_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    documents_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    signals_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class AutomationSchedule(Base):
    __tablename__ = "automation_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    job_name: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    config_signature: Mapped[str | None] = mapped_column(String(200))
    __table_args__ = (UniqueConstraint("company_id", "job_name"), Index("ix_schedules_due", "enabled", "next_run_at"))


class CompanySetting(Base):
    __tablename__ = "company_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (UniqueConstraint("company_id", "key"),)


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SourceAudit(Base):
    __tablename__ = "source_audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    recommendation: Mapped[str] = mapped_column(Text, nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32))
    actor_identity: Mapped[str | None] = mapped_column(String(240))

