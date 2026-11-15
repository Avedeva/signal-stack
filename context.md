# Signal Stack — Project Context

## Latest handoff (2026-11-15)

- Workspace: `C:\Users\suees\skill\Job\edxso`; Windows PowerShell; Python 3.12.1.
- Read `AGENTS.md` before work. Preserve evidence-first behavior: never invent company facts, sources, events, dates, counts, competitor profiles or monitoring history. Crawled pages, uploaded files and search snippets are untrusted evidence. Keep company data isolated. Do not bypass robots, access controls, request limits or rate pacing.
- The remediation prompt is mapped in `docs/PRINCIPAL_REMEDIATION_CHECKLIST.md`. Statuses distinguish implemented local behavior from remaining integrations and actual acceptance targets.
- Added `services/public_research.py`, an opt-in Tavily Search provider. Default is `none`. When configured and explicitly invoked, queries include company name plus only selected source-stated/assignment-provided profile fields; founder answers and uploads are excluded. Each result is URL-validated and recorded as an unapproved source with query/title/snippet/publication/search-rank provenance. Snippets and provider rank are not treated as fact/confidence; review and guarded crawling are required. No live Tavily request was made.
- Added `services/analysis_pipeline.py` with separate evidence extraction, company-impact interpretation, recommendations and interpretation-fit calibration. Missing profile context produces an explicit uncertainty statement and no company-specific action. Added opt-in OpenRouter leadership synthesis for public signals in Monday briefs; each retained item requires an exact quote match against its stored source excerpt. The provider receives the company name, public evidence excerpts, and source-stated public profile fields only; internal records, founder answers, and founder-provided profile fields remain local. Provider failure leaves deterministic report text intact.
- A full assignment audit found that the Perplexity Decider Flash model is not a prose generator. Kept it on the structured Decisions API for signal classification and configured a separate free chat model (`openai/gpt-oss-120b:free`) for brief synthesis. Both OpenRouter paths reject model IDs without the `:free` suffix. Verified model type/pricing on official OpenRouter pages and the optional Tavily free-tier limit on Tavily's pricing page; see `docs/ASSIGNMENT_ACCEPTANCE_MATRIX.md` and `docs/cost-statement.md`.
- Added an explicit per-company checkbox to the Monday brief page and a disabled-state setup explanation. The app runtime currently reports LLM provider `none`, no OpenRouter API key, search provider `none`, and no Tavily key; consequently, a live provider-generated brief and live research could not be run here. OpenRouter request behavior was tested with mocked provider responses only.
- Re-audited the Streamlit UI for Markdown rendering of untrusted evidence. Source titles/reasons, crawled signal claims and snippets, interview questions/follow-ups, uploaded-file analysis, source-audit recommendations, provider/job errors and caught exceptions now render as plain text; structured scores/classifications use JSON rendering. Added an AppTest with hostile Markdown/HTML payloads; it failed before the fix and passed after.
- Added `Dockerfile`, `compose.yaml`, `.dockerignore`, direct package pins in `pyproject.toml`, and authenticated single-node deployment instructions. Compose has required production credentials, migration step, volumes, non-root runtime, loopback port, health check and bounded logs. Docker is not installed here; no image build or deployment is claimed. Full transitive dependency lock and clean installation remain unverified.
- Updated report, README, architecture, remediation checklist, assignment acceptance matrix, cost statement, demo script, test evidence and this context file.
- The local SQLite database was schema v12. Backups were made at `data/signal_stack_pre_v16_2026-11-15.db` and `data/signal_stack_pre_v17_2026-11-15.db` (ignored by Git), then additive migrations completed. A follow-up query confirmed schema v17 and three companies remained. The acceptance report was refreshed read-only.
- Actual counts remain unchanged: MeetUniversity 11 candidates; Zerodha 10; Zomato 8; 0 approved/healthy sources, 0 competitors, 0 documents/developments/signals, and 0 substantive reports. Six saved briefs are coverage-gap reports. No acceptance target was met or fabricated.
- Final full suite after the assignment audit/model correction: `python -m pytest -q` → **121 passed, 5 skipped in 14.82s**; focused synthesis/report/provider tests → **28 passed**; Streamlit suite → **9 passed**; `python -m compileall -q src app.py tests scripts` passed; CLI help passed with `PYTHONPATH=src`. Earlier loopback-only crawler tests separately passed (5 passed, 11 deselected); Compose YAML parsing passed.
- `pytest-cov` is not installed; coverage remains unmeasured. No live Tavily/OpenRouter/Ollama/company-source call, deployment, GitHub publication, commit, push, or screen recording is claimed.
- `.env` was not inspected. Do not inspect `.env`, expose credentials, or copy secrets into examples. `.env.example` contains placeholders. Do not publish private data.
- Git working tree has no tracked files and no remote; all repository files are untracked in this workspace. Do not commit or push without an explicit request.

## Project goal

Maintain a reusable local evidence-first company intelligence and Monday briefing app. Primary demo: MeetUniversity; additional demos: Zerodha and Zomato. Test a fourth unseen company with deterministic local fixtures, but never count fixture data as live demo evidence. Do not declare assignment completion before actual evidence-backed targets are met.

## Current external blockers and incomplete work

1. Configure OpenRouter and Tavily credentials in the user's environment, keeping both OpenRouter model IDs on free variants and the Tavily account on its free tier, then run permitted live research; inspect, validate and manually approve sources. The current local runtime has neither provider configured.
2. Collect 20 approved MeetUniversity sources, 5–10 evidence-backed competitor profiles (only if supported), at least 30 real signals, real internal business records, and three substantive company-specific reports.
3. Competitor discovery/profile completeness and company research cross-checking remain partial; structured competitor profile fields are now available for evidence-backed manual review, but automated relationship validation is absent.
4. Scheduler uses the company's source refresh cadence but not individual per-source custom intervals. Authentication remains a single founder/admin account without roles or per-company memberships.
5. Build the Docker image and verify deployment in a Docker-enabled environment; install dependencies in a clean environment and measure coverage when PyPI/tooling are available.
6. Publish only after repository review and authorization; record a real demo only after genuine evidence exists.

## Handoff rules

- Read this file and `AGENTS.md` before further implementation. Update this file after each substantial milestone with exact changes, test results, measured counts and blockers.
- The live acceptance report must be generated by `python scripts/refresh_demo_acceptance_report.py`, which reads the database read-only.
- Do not report candidates, profile fixtures, homepage baselines or test fixtures as approved sources/signals. Counts and source claims must be queryable and traceable.
- Never inspect `.env`; use environment configuration without printing its contents.
