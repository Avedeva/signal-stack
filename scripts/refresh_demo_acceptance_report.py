"""Refresh the acceptance report from a read-only local SQLite snapshot."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from signal_stack.services.briefs import HEADINGS  # noqa: E402


def _count(connection, sql: str, company_id: int) -> int:
    return int(connection.execute(sql, (company_id,)).fetchone()[0] or 0)


def build_report(connection: sqlite3.Connection, *, checked_on: str | None = None) -> str:
    """Render measured counts from the supplied already-open SQLite connection."""
    connection.row_factory = sqlite3.Row
    checked_on = checked_on or date.today().isoformat()
    schema_version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    companies = connection.execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    lines = [
        "# Demo Acceptance Report", "", f"Checked: {checked_on} using a read-only connection to the current local SQLite database.",
        f"Schema version: {schema_version}.", "",
        "No counts are seeded by this generator; every company value below comes from a database query. A source candidate is not an approved source, and a saved coverage-gap brief is not a substantive intelligence report.", "",
        "| Company | Source candidates | Approved | Healthy | Competitors by status/type | Documents | Developments / signals | Verified developments | Interview progress | Briefs | Scheduled job executions |",
        "|---|---:|---:|---:|---|---:|---:|---:|---|---|---|",
    ]
    total_briefs = total_substantive = total_gaps = heading_valid = 0
    for company in companies:
        cid = company["id"]
        sources = _count(connection, "SELECT COUNT(*) FROM sources WHERE company_id=?", cid)
        approved = _count(connection, "SELECT COUNT(*) FROM sources WHERE company_id=? AND status='approved'", cid)
        healthy = _count(connection, "SELECT COUNT(*) FROM sources WHERE company_id=? AND last_status='healthy'", cid)
        competitor_rows = connection.execute("SELECT status,competitor_type,COUNT(*) AS n FROM competitors WHERE company_id=? GROUP BY status,competitor_type ORDER BY status,competitor_type", (cid,)).fetchall()
        competitor_text = "; ".join(f"{row['status']}/{row['competitor_type']}: {row['n']}" for row in competitor_rows) or "none"
        documents = _count(connection, "SELECT COUNT(*) FROM documents WHERE company_id=?", cid)
        developments = _count(connection, "SELECT COUNT(*) FROM developments WHERE company_id=?", cid)
        signals = _count(connection, "SELECT COUNT(*) FROM signals WHERE company_id=?", cid)
        verified = _count(connection, "SELECT COUNT(*) FROM developments WHERE company_id=? AND verified=1", cid)
        sessions = _count(connection, "SELECT COUNT(*) FROM interview_sessions WHERE company_id=?", cid)
        completed = _count(connection, "SELECT COUNT(*) FROM interview_sessions WHERE company_id=? AND status='completed'", cid)
        answered = _count(connection, "SELECT COUNT(*) FROM interview_questions q JOIN interview_sessions s ON q.session_id=s.id WHERE s.company_id=? AND q.answered_at IS NOT NULL", cid)
        questions = _count(connection, "SELECT COUNT(*) FROM interview_questions q JOIN interview_sessions s ON q.session_id=s.id WHERE s.company_id=?", cid)
        progress = f"{answered}/{questions} answered ({completed}/{sessions} completed)"
        briefs = connection.execute("SELECT signal_count,markdown FROM briefs WHERE company_id=? ORDER BY id", (cid,)).fetchall()
        company_substantive = sum(1 for item in briefs if item["signal_count"] > 0)
        company_gaps = sum(1 for item in briefs if item["signal_count"] == 0 and ("Coverage gap:" in item["markdown"] or "Not assessed:" in item["markdown"]))
        current_heading_count = sum(1 for item in briefs if all(f"## {heading}" in item["markdown"] for heading in HEADINGS))
        total_briefs += len(briefs)
        total_substantive += company_substantive
        total_gaps += company_gaps
        heading_valid += current_heading_count
        brief_text = f"{len(briefs)} ({company_substantive} substantive, {company_gaps} coverage-gap)"
        job_rows = connection.execute("SELECT job_name,status,COUNT(*) AS n FROM scheduled_job_runs WHERE company_id=? GROUP BY job_name,status ORDER BY job_name,status", (cid,)).fetchall()
        job_text = "; ".join(f"{row['job_name']} {row['status']}: {row['n']}" for row in job_rows) or "none"
        lines.append(
            f"| {company['name']} | {sources} | {approved} | {healthy} | {competitor_text} | {documents} | {developments} / {signals} | {verified} | {progress} | {brief_text} | {job_text} |"
        )
    lines.extend([
        "", "## Acceptance status", "",
        f"Across the queried companies: {total_briefs} saved briefs; {total_substantive} substantive briefs with at least one signal; {total_gaps} explicitly identified coverage-gap briefs; {heading_valid} saved briefs contain every current required heading.",
        "The assignment targets of 20 validated and user-approved MeetUniversity public sources, 5–10 evidenced competitor candidates, 30 genuine intelligence records, and three substantive company-specific reports must be met by real evidence. This report does not count unapproved candidates, profile fixture facts, homepage baselines, or test fixtures toward those targets.",
        "", "## Live-operation limits", "",
        "Counts show stored local records only. They do not prove continuous monitoring or future job execution. Public source availability, provider access, and scheduled recurrence must be demonstrated in the deployment environment. No data is inserted or modified by this report generator.", "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "signal_stack.db")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "DEMO_ACCEPTANCE_REPORT.md")
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        parser.error(f"Database does not exist: {database}")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        report = build_report(connection)
    finally:
        connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Refreshed {args.output}; source database opened read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
