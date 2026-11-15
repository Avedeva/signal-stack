from signal_stack.services.analysis_pipeline import (
    AnalysisContext,
    EvidenceRecord,
    analyze_evidence,
)


def test_analysis_keeps_reported_event_and_company_impact_as_distinct_stages():
    evidence = EvidenceRecord(
        source_title="Industry publisher",
        source_url="https://publisher.example/story",
        category="competitors",
        title="Study platform announces a new application service",
        content="The publisher reports a new application service for international students.",
        published_at=None,
    )
    context = AnalysisContext(
        company_name="Study Co",
        profile_key="customer_segments",
        profile_value="international applicants",
        profile_status="founder-provided",
        profile_evidence_url=None,
        industry="education technology",
        industry_provider="deterministic",
        industry_confidence=0.62,
    )

    result = analyze_evidence(evidence, context, "opportunity")

    assert "source text reports" in result.factual_claim.casefold()
    assert "international applicants" in result.interpretation
    assert "international applicants" in result.recommended_action
    assert evidence.source_url in result.factual_claim
    assert result.analysis_method == "deterministic-profile-v2"
    assert result.analysis_confidence <= 0.5
    assert "not a truth or verification score" in result.confidence_rationale.casefold()


def test_analysis_does_not_invent_company_specific_actions_without_profile_context():
    evidence = EvidenceRecord(
        source_title="Publisher",
        source_url="https://publisher.example/story",
        category="market",
        title="A company announced an expansion",
        content="The article reports an expansion, but gives no company-specific details.",
        published_at=None,
    )

    result = analyze_evidence(evidence, AnalysisContext(company_name="Unknown Co"), "neutral")

    assert "company-specific fit is uncertain" in result.interpretation.casefold()
    assert "no company-specific action can be justified" in result.recommended_action.casefold()
    assert "publication date is unknown" in result.confidence_rationale.casefold()

