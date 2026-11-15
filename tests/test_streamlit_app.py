from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture(scope="module", autouse=True)
def isolated_app_database(tmp_path_factory):
    import signal_stack.database as database
    from signal_stack.services.company_research import create_company
    from signal_stack.services.source_discovery import discover_sources

    test_dir = tmp_path_factory.mktemp("test_streamlit_app")
    test_engine = database._engine_for(f"sqlite:///{test_dir / 'signal-stack-app-test.sqlite'}")
    database.init_db(test_engine)
    factory = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        company = create_company(session, "AppTest Example Company", "https://apptest.example")
        discover_sources(session, company.id)
        session.commit()
    finally:
        session.close()

    original_engine = database.engine
    database.engine = test_engine
    database.SessionLocal.configure(bind=test_engine)
    try:
        yield test_engine
    finally:
        database.SessionLocal.configure(bind=original_engine)
        database.engine = original_engine
        test_engine.dispose()


def test_streamlit_app_tests_use_a_temporary_database(isolated_app_database):
    assert Path(isolated_app_database.url.database) != APP_PATH.parent / "data" / "signal_stack.db"


def test_dashboard_starts_and_all_pages_render():
    pages = [
        "Home / Overview", "Company Setup", "Company Intelligence Profile", "Adaptive AI Interview",
        "Competitors", "Signal Stack", "Source Manager", "Intelligence Feed", "Monday Morning Brief",
        "Brief History", "Internal File Upload", "Automation / Job History", "Settings",
    ]
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    assert not app.exception
    assert app.title
    for page in pages:
        app.radio[0].set_value(page).run()
        assert not app.exception, f"Page failed to render: {page}"


def test_company_setup_exposes_resumable_onboarding_button():
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.radio[0].set_value("Company Setup").run()
    assert any(button.label == "Run / resume onboarding" for button in app.button)


def test_company_profile_exposes_evidence_based_classification():
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.radio[0].set_value("Company Intelligence Profile").run()
    assert any(button.label == "Reassess industry and business model" for button in app.button)
    assert any(button.label == "Search for public research sources" and button.disabled for button in app.button)


def test_competitor_page_exposes_evidence_discovery_action():
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.radio[0].set_value("Competitors").run()
    assert any(button.label == "Discover candidates from collected evidence" for button in app.button)


def test_source_manager_exposes_batch_approval_for_validated_candidates():
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.radio[0].set_value("Source Manager").run()
    assert any(button.label == "Approve selected validated sources" for button in app.button)


def test_company_setup_delete_requires_explicit_name_confirmation():
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.radio[0].set_value("Company Setup").run()
    delete_button = next(button for button in app.button if button.label == "Delete company and its data")
    assert delete_button.disabled


def test_monday_brief_exposes_explicit_public_only_llm_synthesis_opt_in():
    app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
    app.radio[0].set_value("Monday Morning Brief").run()
    synthesis = next(item for item in app.checkbox if item.label == "Add OpenRouter synthesis from public evidence")
    assert synthesis.value is False
    assert any(button.label == "Generate and save brief" for button in app.button)
    assert "Internal uploads, founder interview answers" in synthesis.help


def test_untrusted_source_title_and_reason_are_rendered_as_plain_text(isolated_app_database):
    from sqlalchemy import select

    from signal_stack.database import SessionLocal
    from signal_stack.models import Company, Source

    title_payload = "# Injected heading\n[Fake source](javascript:alert(1))"
    reason_payload = "<script>alert('x')</script> [click](javascript:alert(1))"
    session = SessionLocal()
    try:
        company = session.scalar(select(Company).where(Company.name == "AppTest Example Company"))
        source = session.scalar(select(Source).where(Source.company_id == company.id, Source.category == "customers"))
        original_title, original_reason = source.title, source.reason
        source.title, source.reason = title_payload, reason_payload
        session.commit()
    finally:
        session.close()

    try:
        app = AppTest.from_file(str(APP_PATH), default_timeout=20).run()
        app.radio[0].set_value("Signal Stack").run()
        visible_text = "\n".join(element.value for element in app.text)
        visible_markdown = "\n".join(element.value for element in app.markdown)
        assert title_payload in visible_text
        assert reason_payload in visible_text
        assert "javascript:alert(1)" not in visible_markdown
    finally:
        session = SessionLocal()
        try:
            source = session.scalar(select(Source).where(Source.company_id == company.id, Source.category == "customers"))
            source.title, source.reason = original_title, original_reason
            session.commit()
        finally:
            session.close()

