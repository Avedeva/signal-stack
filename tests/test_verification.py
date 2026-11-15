import pytest

from signal_stack.models import Development, Document, Source, VerificationHistory, utcnow
from signal_stack.services.company_research import create_company
from signal_stack.services.verification import transition_verification


def _development(session):
    company = create_company(session, "Verification Co", "https://verification.example")
    source = Source(company_id=company.id, url="https://publisher.example/feed", title="Publisher", category="market", source_type="rss", reason="fixture")
    session.add(source)
    session.flush()
    doc = Document(company_id=company.id, source_id=source.id, canonical_url="https://publisher.example/story", title="Story", content_hash="verification-hash", content="Source evidence", category="market", source_reliability=.8)
    session.add(doc)
    session.flush()
    development = Development(company_id=company.id, document_id=doc.id, what_happened="Reported development", business_implication="Possible context", signal_type="neutral")
    session.add(development)
    session.commit()
    return company, source, doc, development


def test_source_review_and_independent_corroboration_keep_provenance(db_session):
    company, _source, _doc, development = _development(db_session)
    source_review = transition_verification(
        db_session, company.id, development.id, "source-reviewed", reviewer_identity="Reviewer A",
        explanation="Checked the article and confirmed its wording against the original publisher page.",
    )
    corroboration = transition_verification(
        db_session, company.id, development.id, "independently-corroborated", reviewer_identity="Reviewer A",
        explanation="An independent publication reports the same event.", evidence_urls=["https://independent.example/report"],
    )
    db_session.commit()

    assert source_review.previous_status == "not-reviewed"
    assert source_review.new_status == "source-reviewed"
    assert source_review.evidence_urls_json == '["https://publisher.example/story"]'
    assert corroboration.previous_status == "source-reviewed"
    assert development.verified is True
    history = db_session.query(VerificationHistory).filter_by(development_id=development.id).order_by(VerificationHistory.id).all()
    assert [item.reviewer_identity for item in history] == ["Reviewer A", "Reviewer A"]
    assert len(history) == 2


def test_corroboration_rejects_same_domain_and_missing_independent_evidence(db_session):
    company, _source, _doc, development = _development(db_session)
    transition_verification(db_session, company.id, development.id, "source-reviewed", reviewer_identity="Reviewer", explanation="Checked the original publisher article.")
    with pytest.raises(ValueError, match="[Ii]ndependent.*evidence"):
        transition_verification(
            db_session, company.id, development.id, "independently-corroborated", reviewer_identity="Reviewer",
            explanation="I found confirmation.", evidence_urls=["https://publisher.example/another-story"],
        )
    with pytest.raises(ValueError, match="[Ii]ndependent.*evidence"):
        transition_verification(
            db_session, company.id, development.id, "independently-corroborated", reviewer_identity="Reviewer",
            explanation="I have no supporting evidence.",
        )


def test_disputes_require_explanation_and_every_transition_requires_reviewer(db_session):
    company, _source, _doc, development = _development(db_session)
    with pytest.raises(ValueError, match="Reviewer"):
        transition_verification(db_session, company.id, development.id, "source-reviewed", reviewer_identity="", explanation="Checked")
    with pytest.raises(ValueError, match="explanation"):
        transition_verification(db_session, company.id, development.id, "disputed", reviewer_identity="Reviewer", explanation="No")

    record = transition_verification(
        db_session, company.id, development.id, "disputed", reviewer_identity="Reviewer",
        explanation="The publisher corrected the article and withdrew the original claim.",
    )
    db_session.commit()
    assert record.new_status == "disputed"
    assert development.verified is False


def test_verification_transition_cannot_cross_company_or_repeat_same_state(db_session):
    company, _source, _doc, development = _development(db_session)
    other = create_company(db_session, "Other Co", "https://other-verification.example")
    with pytest.raises(ValueError, match="not found"):
        transition_verification(db_session, other.id, development.id, "source-reviewed", reviewer_identity="Reviewer", explanation="Checked original page")
    with pytest.raises(ValueError, match="same state"):
        transition_verification(db_session, company.id, development.id, "not-reviewed", reviewer_identity="Reviewer", explanation="There is no requested status change.")
