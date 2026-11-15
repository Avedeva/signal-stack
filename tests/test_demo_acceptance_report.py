from scripts.refresh_demo_acceptance_report import build_report
from signal_stack.models import Brief, Source, utcnow
from signal_stack.services.company_research import create_company


def test_acceptance_report_builds_company_counts_from_read_only_database_rows(db_session):
    company = create_company(db_session, "Measured Co", "https://measured.example")
    db_session.add(Source(company_id=company.id, url="https://measured.example/news", title="News", category="market", source_type="website", reason="Profile-derived candidate", reliability=.5, status="candidate"))
    now = utcnow()
    db_session.add(Brief(company_id=company.id, period_start=now, period_end=now, markdown="# Monday brief\n\nCoverage gap: no evidence", signal_count=0))
    db_session.commit()
    connection = db_session.connection().connection.driver_connection
    report = build_report(connection, checked_on="2026-11-14")
    assert "| Measured Co | 1 | 0 | 0 | none | 0 | 0 / 0 | 0 | 0/0 answered (0/0 completed) | 1 (0 substantive, 1 coverage-gap) | none |" in report
    assert "No counts are seeded by this generator" in report
