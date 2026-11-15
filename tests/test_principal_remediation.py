from signal_stack.models import Source, SourceAudit
from signal_stack.services.company_research import create_company
from signal_stack.services.source_discovery import assess_source_relevance, set_source_status, upsert_source


def test_source_relevance_does_not_score_discovery_reason_as_evidence(db_session):
    company = create_company(db_session, "Customer Analytics Co", "https://company.example")
    profile = {"company_description": "customer analytics services for retail payments"}
    candidate = Source(
        company_id=company.id,
        url="https://unrelated.example/news",
        title="Local weather report",
        category="market",
        source_type="website",
        reason="Query based on customer analytics retail payments discovery context",
    )

    score_with_query_text = assess_source_relevance(candidate, profile)
    candidate.reason = "Candidate came from a profile-derived query"
    score_without_query_text = assess_source_relevance(candidate, profile)

    assert score_with_query_text == score_without_query_text


def test_source_upsert_refreshes_discovery_fields_and_preserves_review_history(db_session):
    company = create_company(db_session, "Upsert Co", "https://upsert.example")
    source = Source(
        company_id=company.id,
        url="https://publisher.example/feed",
        title="Old name",
        category="market",
        source_type="website",
        reason="Old query",
        reliability=.2,
        relevance=.9,
        status="approved",
        last_status="healthy",
    )
    db_session.add(source)
    db_session.flush()
    audit = SourceAudit(company_id=company.id, source_id=source.id, status="approved", recommendation="Reviewer approved after validation")
    db_session.add(audit)
    db_session.commit()

    updated = upsert_source(
        db_session,
        company.id,
        url=source.url,
        title="Current name",
        category="customers",
        source_type="rss",
        reason="Current discovery query",
        reliability=.8,
    )
    db_session.commit()

    assert updated.id == source.id
    assert updated.title == "Current name"
    assert updated.category == "customers"
    assert updated.source_type == "rss"
    assert updated.reason == "Current discovery query"
    assert updated.reliability == .8
    assert updated.status == "approved"
    assert updated.last_status == "healthy"
    assert db_session.get(SourceAudit, audit.id).recommendation == "Reviewer approved after validation"


def test_source_approval_records_reviewer_and_previous_state(db_session):
    from signal_stack.models import utcnow
    company = create_company(db_session, "Review History Co", "https://review-history.example")
    source = Source(company_id=company.id, url="https://publisher.example/feed", title="Feed", category="market", source_type="rss", reason="Candidate", status="candidate", last_status="healthy", last_checked_at=utcnow())
    db_session.add(source)
    db_session.flush()
    db_session.info["principal"] = "alice@example.test"

    approved = set_source_status(db_session, company.id, source.id, "approved")
    audit = db_session.query(SourceAudit).filter_by(source_id=source.id, status="review-approved").one()

    assert approved.next_due_at is not None
    assert audit.previous_status == "candidate"
    assert audit.actor_identity == "alice@example.test"
