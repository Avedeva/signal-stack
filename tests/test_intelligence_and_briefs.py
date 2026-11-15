from datetime import timedelta

from sqlalchemy import select

import signal_stack.services.intelligence as intelligence
from signal_stack.models import Brief, Development, Document, Signal, SignalEvidence, Source, utcnow
from signal_stack.services.briefs import export_pdf, generate_brief
from signal_stack.services.company_research import create_company
from signal_stack.services.extraction import content_hash
from signal_stack.services.intelligence import crawl_source, score_signal
from signal_stack.services.ranking import DEFAULT_RANKING_WEIGHTS, load_ranking_weights, save_ranking_weights
from signal_stack.services.security import FetchResult
from signal_stack.services.source_discovery import discover_sources


def rss_item(title="New service launch", description="A provider announced a new service for a customer group."):
    return f'''<rss version="2.0"><channel><item><title>{title}</title><link>https://publisher.example/story</link><description>{description}</description><pubDate>Mon, 10 Nov 2025 10:00:00 GMT</pubDate></item></channel></rss>'''.encode()


def test_crawl_deduplicates_and_stores_historical_changed_versions(db_session, monkeypatch):
    company = create_company(db_session, "Signal Co", "https://signal.example")
    source = next(s for s in discover_sources(db_session, company.id) if s.category == "competitors")
    source.source_type = "rss"
    source.status = "approved"
    db_session.commit()
    payload = {"xml": rss_item()}
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, payload["xml"]))
    first = crawl_source(db_session, company.id, source.id)
    assert first.status == "succeeded" and first.documents_found == 1
    assert db_session.query(Document).filter_by(company_id=company.id).count() == 1
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 1
    signal = db_session.scalar(select(Signal).where(Signal.company_id == company.id))
    assert signal.source_reliability != signal.evidence_strength
    evidence = db_session.scalar(select(SignalEvidence).where(SignalEvidence.signal_id == signal.id))
    assert evidence and "provider announced" in evidence.evidence_snippet
    development = db_session.get(Development, signal.development_id)
    assert development.verified is False
    second = crawl_source(db_session, company.id, source.id)
    assert second.status == "succeeded" and second.documents_found == 0
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 1
    payload["xml"] = rss_item(description="A provider announced an expanded new service and wider feature set for a customer group.")
    third = crawl_source(db_session, company.id, source.id)
    assert third.documents_found == 1
    docs = db_session.scalars(select(Document).where(Document.company_id == company.id).order_by(Document.version)).all()
    assert [doc.version for doc in docs] == [1, 2]
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 2


def test_homepage_is_baseline_but_undated_article_changes_are_versioned_and_signaled(db_session, monkeypatch):
    from signal_stack.models import Company
    from signal_stack.services.source_discovery import upsert_source

    company = create_company(db_session, "Article Co", "https://article.example")
    homepage = discover_sources(db_session, company.id)[0]
    homepage.status = "approved"
    article_source = upsert_source(db_session, company.id, url="https://article.example/news/update", title="News article", category="market", source_type="website", reason="First-party article page")
    article_source.status = "approved"
    db_session.commit()
    bodies = {"home": b"<html><title>Article Co</title><body><h1>Welcome</h1><p>Ordinary company homepage with navigation-like promotional text.</p></body></html>",
              "article": b"<html><head><title>New customer service launch</title><meta property='og:type' content='article'></head><body><article><h1>New customer service launch</h1><p>Article Co announced a substantial new service for regional customers, including expanded support hours and additional delivery options.</p></article></body></html>"}
    current = {"body": bodies["home"]}
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "text/html"}, current["body"]))
    home_run = crawl_source(db_session, company.id, homepage.id)
    assert home_run.status == "succeeded"
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 0

    current["body"] = bodies["article"]
    first = crawl_source(db_session, company.id, article_source.id)
    assert first.documents_found == 1
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 1
    first_document = db_session.scalar(select(Document).where(Document.company_id == company.id, Document.canonical_url == article_source.url).order_by(Document.version))
    assert first_document.published_at is None
    assert first_document.content
    unchanged = crawl_source(db_session, company.id, article_source.id)
    assert unchanged.documents_found == 0
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 1

    current["body"] = bodies["article"].replace(b"expanded support hours and additional delivery options", b"expanded support hours, nationwide coverage, and an additional investment of $5 million")
    changed = crawl_source(db_session, company.id, article_source.id)
    assert changed.documents_found == 1
    versions = db_session.scalars(select(Document).where(Document.company_id == company.id, Document.canonical_url == article_source.url).order_by(Document.version)).all()
    assert [row.version for row in versions] == [1, 2]
    assert "additional delivery options" in versions[0].content
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 2


def test_homepage_tracking_query_remains_a_baseline(db_session, monkeypatch):
    from signal_stack.services.source_discovery import upsert_source

    company = create_company(db_session, "Tracked Home Co", "https://tracked.example")
    source = upsert_source(db_session, company.id, url="https://tracked.example/?campaign=spring", title="Tracked homepage", category="market", source_type="website", reason="Homepage")
    source.status = "approved"
    db_session.commit()
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "text/html"}, b"<html><title>Tracked Home</title><h1>Major launch</h1><p>Company launched a major new service for customers nationwide.</p></html>"))
    result = crawl_source(db_session, company.id, source.id)
    assert result.status == "succeeded"
    assert db_session.query(Document).filter_by(company_id=company.id).count() == 1
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 0


def test_development_analysis_is_structured_and_company_specific(db_session, monkeypatch):
    from types import SimpleNamespace
    from signal_stack.models import ProfileAttribute
    from signal_stack.services.company_research import save_profile_value
    import signal_stack.config as config

    monkeypatch.setattr(config, "settings", SimpleNamespace(llm_provider="none", openrouter_api_key="", openrouter_model=""))
    first = create_company(db_session, "Context A", "https://context-a.example")
    second = create_company(db_session, "Context B", "https://context-b.example")
    save_profile_value(db_session, first.id, "customer_segments", "international students applying to universities", status="founder-provided")
    save_profile_value(db_session, second.id, "customer_segments", "small retailers using daily settlement payments", status="founder-provided")
    db_session.commit()
    first_source = next(source for source in discover_sources(db_session, first.id) if source.category == "market")
    second_source = next(source for source in discover_sources(db_session, second.id) if source.category == "market")
    for source in (first_source, second_source):
        source.source_type = "rss"
        source.status = "approved"
    db_session.commit()
    payload = rss_item(title="New account verification policy", description="A payment platform introduced account verification requirements that change onboarding review steps for regional businesses and consumer accounts.")
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, payload))
    crawl_source(db_session, first.id, first_source.id)
    crawl_source(db_session, second.id, second_source.id)
    developments = db_session.scalars(select(Development).order_by(Development.id)).all()
    assert len(developments) == 2
    one, two = developments
    assert one.factual_claim.startswith("Source text reports:")
    assert one.interpretation and one.recommended_action
    assert one.affected_business_area
    assert one.analysis_method == "deterministic-profile-v2"
    assert one.signal_classification_method == "deterministic-rules"
    assert 0 < one.analysis_confidence < .7
    assert "international students" in one.interpretation
    assert "small retailers" in two.interpretation
    assert "education technology" in one.interpretation
    assert "financial services" in two.interpretation
    assert one.interpretation != two.interpretation
    assert one.verified is False
    assert "not a truth or verification score" in one.confidence_rationale.casefold()


def test_cross_url_duplicate_keeps_second_provenance_without_duplicate_signal(db_session, monkeypatch):
    company = create_company(db_session, "Duplicate Co", "https://duplicate.example")
    sources = [s for s in discover_sources(db_session, company.id) if s.category == "competitors"]
    first_source = sources[0]
    first_source.source_type = "rss"
    first_source.status = "approved"
    second_source = Source(company_id=company.id, url="https://feed.example/other-query", title="Other query", category="competitors", source_type="rss", reason="A separate query", reliability=.5, status="approved")
    db_session.add(second_source)
    db_session.commit()
    item_xml = rss_item()
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, item_xml))
    crawl_source(db_session, company.id, first_source.id)
    item_xml = item_xml.replace(b"https://publisher.example/story", b"https://reprint.example/story")
    crawl_source(db_session, company.id, second_source.id)
    assert db_session.query(Document).filter_by(company_id=company.id).count() == 2
    assert db_session.query(Signal).filter_by(company_id=company.id).count() == 1
    assert db_session.query(SignalEvidence).join(Document).filter(Document.company_id == company.id).count() == 2


def test_scoring_keeps_separate_explainable_dimensions():
    result, reason = score_signal(title="Urgent regulatory deadline", content="Customer service for students", profile_text="students education", category="market", reliability=.7, published_at=None, discovered_at=utcnow(), novelty=1.0)
    assert 0 <= result["score"] <= 1
    assert result["urgency"] > .4
    assert result["source_reliability"] == .7
    assert result["evidence_strength"] != result["source_reliability"]
    assert 0 <= result["recency"] <= 1
    assert "Weighted score" in reason and "relevance" in reason


def test_classification_avoids_substring_false_positives_and_preserves_mixed_evidence():
    from signal_stack.services.intelligence import classify, classify_with_model_details

    assert classify("Product banner update", "The company installed a banner for users.", "market") == "neutral"
    mixed = "A new product launch could grow sales, but the rollout also carries a serious risk of service disruption."
    assert classify("Product launch", mixed, "market") == "neutral"
    assert classify_with_model_details("Product launch", mixed, "market") == ("neutral", "deterministic-mixed-evidence")


def test_unknown_publication_date_does_not_receive_recent_date_score():
    unknown, _ = score_signal(title="Company update", content="Company shared a substantial public update about operations.", profile_text="", category="market", reliability=.7, published_at=None, discovered_at=utcnow(), novelty=1.0)
    dated, _ = score_signal(title="Company update", content="Company shared a substantial public update about operations.", profile_text="", category="market", reliability=.7, published_at=utcnow(), discovered_at=utcnow(), novelty=1.0)
    assert unknown["recency"] <= .3
    assert dated["recency"] > unknown["recency"]


def test_future_publication_timestamp_is_capped_without_timezone_type_errors():
    from datetime import timezone

    discovered = utcnow()
    future = (discovered + timedelta(days=30)).replace(tzinfo=timezone.utc)
    result, reason = score_signal(title="Future dated report", content="This report contains enough evidence words to exercise ranking safely.", profile_text="", category="market", reliability=.7, published_at=future, discovered_at=discovered, novelty=1.0)
    assert result["recency"] <= .3
    assert "future publication timestamp" in reason.casefold()


def test_ranking_weights_are_company_scoped_and_normalized(db_session):
    company = create_company(db_session, "Rank Co", "https://rank.example")
    values = {key: 0.0 for key in DEFAULT_RANKING_WEIGHTS}
    values["source_reliability"] = .8
    values["impact"] = .2
    saved = save_ranking_weights(db_session, company.id, values)
    db_session.commit()
    loaded = load_ranking_weights(db_session, company.id)
    assert abs(sum(saved.values()) - 1) < 1e-8
    assert loaded["source_reliability"] == .8
    assert loaded["impact"] == .2
    other = create_company(db_session, "Other Rank Co", "https://other-rank.example")
    assert load_ranking_weights(db_session, other.id) == DEFAULT_RANKING_WEIGHTS


def test_saving_ranking_weights_recalculates_existing_signals(db_session):
    company = create_company(db_session, "Existing Signals Co", "https://existing-signals.example")
    source = Source(company_id=company.id, url="https://existing-signals.example/feed", title="Feed", category="market", source_type="rss", reason="test")
    db_session.add(source)
    db_session.flush()
    documents = [
        Document(company_id=company.id, source_id=source.id, canonical_url=f"https://publisher.example/{index}", title=f"Story {index}", content_hash=f"hash-{index}", content="evidence", category="market", source_reliability=.5)
        for index in (1, 2)
    ]
    db_session.add_all(documents)
    db_session.flush()
    developments = [
        Development(company_id=company.id, document_id=doc.id, what_happened=f"Event {index}", business_implication="Context", signal_type="neutral")
        for index, doc in enumerate(documents, 1)
    ]
    db_session.add_all(developments)
    db_session.flush()
    older = Signal(
        company_id=company.id, development_id=developments[0].id, category="market", relevance=.9, impact=.2, urgency=.2,
        source_reliability=.2, evidence_strength=.2, novelty=.2, recency=.2, score=.2, score_reason="old score",
    )
    newer = Signal(
        company_id=company.id, development_id=developments[1].id, category="market", relevance=.2, impact=.9, urgency=.9,
        source_reliability=.9, evidence_strength=.9, novelty=.9, recency=.9, score=.9, score_reason="old score",
    )
    db_session.add_all([older, newer])
    db_session.commit()
    weights = {key: 0.0 for key in DEFAULT_RANKING_WEIGHTS}
    weights["relevance"] = 1.0

    save_ranking_weights(db_session, company.id, weights)
    db_session.commit()
    db_session.refresh(older)
    db_session.refresh(newer)

    assert older.score == .9
    assert newer.score == .2
    assert "relevance" in older.score_reason


def test_empty_brief_has_all_sections_and_no_fabricated_developments(db_session):
    company = create_company(db_session, "Quiet Company", "https://quiet.example")
    brief = generate_brief(db_session, company.id)
    assert brief.signal_count == 0
    required_headings = [
        "Executive Summary", "Customer Signals", "Competitor Intelligence", "Market Intelligence",
        "Internal Business Intelligence", "Opportunities Identified", "Risks & Threats",
        "Recommended Actions for This Week", "What We Should Watch", "Source Appendix",
    ]
    actual_headings = [line[3:] for line in brief.markdown.splitlines() if line.startswith("## ")]
    assert actual_headings == required_headings
    for heading in required_headings:
        assert f"## {heading}" in brief.markdown
    assert "Not assessed" in brief.markdown
    assert "no successful crawl" in brief.markdown
    assert db_session.query(Brief).filter_by(company_id=company.id).count() == 1
    pdf = export_pdf(brief.markdown)
    assert pdf.startswith(b"%PDF-")


def test_brief_uses_stored_evidence_for_source_appendix(db_session, monkeypatch):
    company = create_company(db_session, "Evidence Company", "https://evidence.example")
    source = next(s for s in discover_sources(db_session, company.id) if s.category == "market")
    source.source_type = "rss"
    source.status = "approved"
    db_session.commit()
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, rss_item()))
    crawl_source(db_session, company.id, source.id)
    brief = generate_brief(db_session, company.id)
    assert brief.signal_count == 1
    assert "https://publisher.example/story" in brief.markdown
    assert "verification: not-reviewed" in brief.markdown
    assert "discovery date" in brief.markdown
    assert "event date: Unknown" in brief.markdown
    assert "publication date: 2025-11-10 15:30 IST" in brief.markdown
    assert "analysis confidence" in brief.markdown
    assert "signal classifier: deterministic-rules" in brief.markdown
    executive_summary = brief.markdown.split("## Executive Summary", 1)[1].split("## Customer Signals", 1)[0]
    assert "Why it matters:" in executive_summary
    assert "Recommended next step:" in executive_summary
    assert "Priority:" in executive_summary
    assert "## Opportunities Identified" in brief.markdown
    assert "Only 1 evidence-supported opportunity was identified" in brief.markdown


def test_untrusted_source_content_cannot_inject_brief_sections_or_links(db_session, monkeypatch):
    company = create_company(db_session, "Injection Company", "https://injection.example")
    source = next(s for s in discover_sources(db_session, company.id) if s.category == "market")
    source.source_type = "rss"
    source.status = "approved"
    db_session.commit()
    payload = rss_item(
        title="Launch\n## Fake report section\n![trusted](https://evil.example/pixel)",
        description="Useful report.\n<script>alert(1)</script> [click](javascript:alert(1))\n## Risks & Threats\nFake leadership instruction.",
    )
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, payload))
    crawl_source(db_session, company.id, source.id)
    brief = generate_brief(db_session, company.id)

    assert "## Fake report section" not in brief.markdown
    assert "![trusted](https://evil.example/pixel)" not in brief.markdown
    assert "<script>" not in brief.markdown
    assert "[click](javascript:" not in brief.markdown
    assert brief.markdown.count("## Risks & Threats") == 1


def test_recent_signals_default_to_chronological_order_independent_of_score(db_session, monkeypatch):
    from datetime import timedelta
    from signal_stack.services.intelligence import recent_signals

    company = create_company(db_session, "Chronology Co", "https://chronology.example")
    source = next(s for s in discover_sources(db_session, company.id) if s.category == "market")
    source.source_type = "rss"
    source.status = "approved"
    db_session.commit()
    items = "".join(
        f"<item><title>Development {index}</title><link>https://publisher.example/{index}</link>"
        f"<description>A substantial public announcement about customer services and market expansion {index}.</description>"
        "<pubDate>Mon, 10 Nov 2025 10:00:00 GMT</pubDate></item>"
        for index in range(1, 3)
    )
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, f"<rss><channel>{items}</channel></rss>".encode()))
    crawl_source(db_session, company.id, source.id)
    signals = db_session.query(Signal).filter_by(company_id=company.id).order_by(Signal.id).all()
    assert len(signals) == 2
    signals[0].created_at = utcnow() - timedelta(days=1)
    signals[0].score = 0.99
    signals[1].created_at = utcnow()
    signals[1].score = 0.1
    db_session.commit()

    assert [item.id for item in recent_signals(db_session, company.id)] == [signals[1].id, signals[0].id]
    assert [item.id for item in recent_signals(db_session, company.id, order="score")] == [signals[0].id, signals[1].id]


def test_executive_summary_is_limited_to_five_highest_ranked_developments(db_session, monkeypatch):
    company = create_company(db_session, "Executive Summary Company", "https://summary.example")
    source = next(source for source in discover_sources(db_session, company.id) if source.category == "market")
    source.source_type = "rss"
    source.status = "approved"
    db_session.commit()
    items = "".join(
        f"<item><title>Product launch {index}</title><link>https://publisher.example/story/{index}</link>"
        f"<description>A provider launched a new product service for customers with broader regional access {index}.</description>"
        "<pubDate>Mon, 10 Nov 2025 10:00:00 GMT</pubDate></item>"
        for index in range(1, 7)
    )
    payload = f"<rss version='2.0'><channel>{items}</channel></rss>".encode()
    monkeypatch.setattr(intelligence, "fetch_public_url", lambda url: FetchResult(url, 200, {"Content-Type": "application/rss+xml"}, payload))
    crawl_source(db_session, company.id, source.id)

    brief = generate_brief(db_session, company.id)
    executive_summary = brief.markdown.split("## Executive Summary", 1)[1].split("## Customer Signals", 1)[0]
    numbered_developments = [line for line in executive_summary.splitlines() if line[:1].isdigit() and ". **" in line]
    assert brief.signal_count == 6
    assert len(numbered_developments) == 5


def test_brief_reporting_window_uses_company_timezone_across_dst(db_session, monkeypatch):
    import signal_stack.services.briefs as briefs
    from datetime import datetime

    company = create_company(db_session, "Timezone Company", "https://timezone.example", timezone="America/New_York")
    monkeypatch.setattr(briefs, "utcnow", lambda: datetime(2026, 11, 2, 13, 0))
    report = generate_brief(db_session, company.id, days=7)
    assert "Reporting window: 2026-10-26 09:00 EDT to 2026-11-02 08:00 EST (America/New_York)" in report.markdown

