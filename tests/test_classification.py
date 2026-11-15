from signal_stack.models import CompanyClassification
from signal_stack.services.classification import classify_company_context
from signal_stack.services.company_research import create_company, save_profile_value


def _make_company(session, name, website, description, **fields):
    company = create_company(session, name, website)
    save_profile_value(session, company.id, "company_description", description, status="source-stated", evidence_url=website, evidence_snippet=description)
    for key, value in fields.items():
        save_profile_value(session, company.id, key, value, status="founder-provided")
    session.commit()
    return company


def test_classification_uses_company_evidence_for_multiple_industries_without_name_rules(db_session):
    cases = [
        ("North Star", "https://health.example", "Clinics provide telemedicine, diagnostics, and patient records for hospitals."),
        ("North Star", "https://learn.example", "Online learning platform offering exam preparation and student courses for universities."),
        ("North Star", "https://finance.example", "Brokerage platform for securities trading, investment accounts, and retail investors."),
        ("North Star", "https://food.example", "Restaurant marketplace for food delivery, local dining, and grocery orders."),
        ("North Star", "https://climate.example", "Carbon accounting and renewable energy software for industrial emissions reporting."),
    ]
    results = []
    for name, website, description in cases:
        company = _make_company(db_session, name, website, description)
        results.append(classify_company_context(db_session, company.id))
    industries = {item.industry for item in results}
    assert len(industries) == len(cases)
    assert results[-1].industry == "climate technology"
    assert all(item.evidence_json and item.confidence > 0 and item.provider == "deterministic-profile" for item in results)


def test_unknown_classification_stays_unknown_and_records_uncertainty(db_session):
    company = _make_company(db_session, "Famous HealthCare University Bank", "https://unknown.example", "We make tools for teams to organize their work.")
    result = classify_company_context(db_session, company.id)
    assert result.industry is None
    assert result.sub_industry is None
    assert result.confidence == 0
    assert "industry" in result.uncertainty


def test_reclassification_tracks_profile_changes_and_keeps_tenant_evidence_isolated(db_session):
    first = _make_company(db_session, "Same Name", "https://first.example", "Clinics provide medical records and patient diagnostics.")
    second = _make_company(db_session, "Same Name", "https://second.example", "Brokerage enables securities trading and investment accounts.")
    health = classify_company_context(db_session, first.id)
    finance = classify_company_context(db_session, second.id)
    save_profile_value(db_session, first.id, "company_description", "Online learning platform for university students and exam preparation.", status="founder-provided", evidence_url=first.website)
    db_session.commit()
    updated = classify_company_context(db_session, first.id)
    assert health.industry != finance.industry
    assert updated.industry != health.industry
    assert "second.example" not in updated.evidence_json
    assert db_session.query(CompanyClassification).filter_by(company_id=first.id).count() == 2


def test_optional_local_classification_requires_verbatim_profile_evidence(db_session, monkeypatch):
    import signal_stack.providers.llm.ollama as ollama

    company = _make_company(db_session, "Unknown Brand", "https://unknown-brand.example", "We build climate reporting software for industrial teams.")
    monkeypatch.setattr(ollama, "classify_company_profile", lambda profile: {
        "industry": "climate reporting software", "sub_industry": "industrial reporting", "business_model": "software licensing",
        "evidence": [
            {"classification_field": "industry", "profile_key": "company_description", "quote": "climate reporting software"},
            {"classification_field": "sub_industry", "profile_key": "company_description", "quote": "industrial teams"},
            {"classification_field": "business_model", "profile_key": "company_description", "quote": "software"},
        ],
    })
    modeled = classify_company_context(db_session, company.id, use_llm=True)
    assert modeled.industry == "climate reporting software"
    assert modeled.provider == "ollama-local"
    assert "climate reporting software" in modeled.evidence_json

    second = _make_company(db_session, "Another Unknown", "https://another-unknown.example", "We make tools for teams to organize field work.")
    monkeypatch.setattr(ollama, "classify_company_profile", lambda profile: {
        "industry": "medical device manufacturing", "sub_industry": None, "business_model": None,
        "evidence": [{"classification_field": "industry", "profile_key": "company_description", "quote": "medical device manufacturing"}],
    })
    fallback = classify_company_context(db_session, second.id, use_llm=True)
    assert fallback.industry is None
    assert fallback.provider == "deterministic-fallback"
    assert "fallback used" in fallback.uncertainty
