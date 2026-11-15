# Principal Remediation Checklist

Reviewed: 2026-11-15. Statuses below describe repository behavior and test evidence, not live-company acceptance.

| Finding | Implementation and tests | Status |
|---|---|---|
| 2.1 Markdown injection | `services/briefs.py` escapes external fields, validates source URLs, and sanitizes persisted reports; `app.py` renders source, signal, interview, upload, audit and error text as plain text. Covered by adversarial report tests and `tests/test_streamlit_app.py::test_untrusted_source_title_and_reason_are_rendered_as_plain_text`. | Implemented locally |
| 2.2 Authentication and authorization | `auth.py` provides salted PBKDF2, timeout, logout, and fail-closed production config; `database.py` requires an ORM principal in production; CLI/scheduler use explicit service identities. `tests/test_auth.py` proves unauthenticated ORM reads fail. One founder/admin account only; no per-company memberships or roles. | Partial: single account boundary, not multi-user authorization |
| 3.1 Total crawler deadline | `services/security.py` uses a killable DNS subprocess and a monotonic deadline across fetch phases; response body reads are deadline-aware and bounded. `tests/test_security_and_extraction.py` uses local slow-header, trickle, hang, redirect, forbidden redirect, and large-body cases. | Implemented locally; five local-server cases passed with loopback access |
| 3.2 Bounded caches and pacing | `BoundedTTLCache` bounds robots entries; `HostPacer` bounds per-host pacing state and avoids sleeping under the global map lock; striped robots locks avoid unbounded lock growth. Cache/pacing tests cover TTL, capacity and concurrency. | Implemented locally |
| 3.3 Scheduler transaction cleanup | `scheduler.execute_job` closes claim and execution sessions in failure paths, rolls back failures, and persists job failure state separately. Failure injection tests cover acquire, begin, execution and commit. | Implemented locally |
| 4.1 Chronological feed | `recent_signals` defaults to newest-first with deterministic ties; score and oldest orders are options. Feed tests cover order and filters. | Implemented locally |
| 4.2 Existing ranking weights | `services/ranking.py` transactionally recalculates active scores from stored dimensions after weight changes, without changing evidence/classification. Ranking tests cover recalculation and ordering. | Implemented locally |
| 5.1 OpenRouter injection/output validation | `providers/llm/openrouter.py` uses the Perplexity Decider model only through the Decisions API and treats evidence as untrusted. `providers/llm/brief_synthesis.py` uses a separate free text model, accepts only known public signal IDs and exact evidence quotes, and filters internal records/founder context. Both paths refuse model IDs without `:free`; errors leave deterministic output. Tests are in `tests/test_openrouter.py` and `tests/test_brief_synthesis.py`. | Implemented locally; live provider call not configured or claimed |
| 5.2 Multi-stage analysis | `services/analysis_pipeline.py` separates event evidence extraction, company-impact interpretation, actions and confidence calibration; the optional brief synthesis provides quote-anchored leadership framing for public signals. Classification, ranking, verification and rendering remain separate. | Partial: core stages are implemented; broader company-specific analysis and live evidence coverage remain limited |
| 6.1 Source relevance inflation | `source_discovery.py` excludes discovery reason from score inputs and reports separate reliability/relevance explanation. `tests/test_principal_remediation.py` checks self-referential scoring. | Implemented locally |
| 6.2 Source upsert ownership | Upserts refresh discovery-owned title/category/type/reason/reliability/relevance while preserving review state and crawl health. Approval transitions append actor/previous-state audit. | Implemented locally |
| 6.3 Company research | `services/public_research.py` adds opt-in Tavily search, schema/URL validation, bounded results, and per-result query/title/snippet/publication provenance. Only source-stated profile fields are sent. Results remain unapproved sources; snippets are not company facts. Homepage research remains first-party. No provider call or independent claim cross-check was performed here. | Partial: provider integration is ready; broader verified research needs credentials, live results and review |
| 7 Competitors | Optional public search can create competitor-source candidates; after a reviewer approves and crawls them, document extraction finds explicit comparisons, rejects role/negation false positives, deduplicates normalized entity names, and stores cited candidates. Schema/UI support editable product, market, segment, geography, relationship, supporting-source URLs, and discovery date fields; unsupported values stay blank. Website domain duplicates are blocked during edits. | Partial: full query/profile cross-check automation is absent; 5–10 MeetUniversity profiles are not present or demonstrated |
| 8 Internal intelligence | `services/internal_analysis.py` analyzes CSV/XLSX rows, mappings, dates, values, segments, statuses, outcomes, themes, duplicates and sample limitations. PDF/TXT use extracted text and limited deterministic themes. `tests/test_internal_analysis.py` uses real fixture rows and checks low sample quality. | Partial: structured data path implemented; no actual business upload in demo database |
| 9 Verification | `services/verification.py` supports all four states, reviewer/explanation provenance, distinct-source/domain corroboration and immutable history; UI exposes transitions/history. `tests/test_verification.py` covers invalid transitions and evidence requirements. | Implemented locally |
| 10 Refresh scheduling | Company `source_refresh_hours` controls due checks; per-source claims/leases and bounded retry backoff are persisted. Scheduler tests cover hourly/daily/weekly/monthly settings and competing claims. Per-source custom cadence is not implemented. | Partial |
| 11 Live acceptance targets | Current values are in `DEMO_ACCEPTANCE_REPORT.md`; candidates, browser-stated context and test fixtures are not counted as approved evidence. | Not met: 20 approved sources, 5–10 competitors, 30 signals and three substantive reports are all absent |
| 12 Monday brief | Ten required headings, explicit empty/coverage-gap language, evidence appendix, dates, confidence, verification, actions and owner suggestions are generated from stored rows. The page offers opt-in OpenRouter synthesis for public evidence, with exact quote matching and deterministic fallback. Persisted reports are sanitized at render/export. | Functionality implemented locally; no provider key or live signals are configured, so a decision-ready company brief is not demonstrated |
| 13.1 README | Setup, configuration, workflows, tests, security, providers, current limits and deployment steps are documented. | Updated; clean install is unverified |
| 13.2 Deployment | `Dockerfile` uses a non-root user; `compose.yaml` requires production auth secrets, uses a migration job, persistent local volumes, loopback-only host binding, health check and bounded logs. Direct package dependencies are pinned. | Prepared, not built or deployed; Docker is unavailable here and transitive dependency lock is absent |
| 13.3 GitHub | `.gitignore` and `.dockerignore` exclude secrets/private data. The working tree has no tracked files and no Git remote. | Not published; repository has not been pushed |
| 13.4 Recording | `docs/demo-script.md` describes an evidence-honest workflow and current limitations. | Script exists; no recording exists |
| 14 Code quality | Provider, extraction, analysis, ranking, verification and report modules have separate contracts. Additive migrations preserve old rows. | Partial; repository-wide lint/type-check/build checks are not configured or verified |

## Regression coverage map

| Regression area | Test location |
|---|---|
| Markdown injection and saved-brief sanitization | `tests/test_intelligence_and_briefs.py` |
| Authentication/session and ORM principal | `tests/test_auth.py` |
| HTTP deadline, trickle, redirects/SSRF and cache/pacing | `tests/test_security_and_extraction.py` |
| Scheduler cleanup, refresh cadence and overlap | `tests/test_uploads_and_scheduler.py` |
| Feed order and ranking recalculation | `tests/test_intelligence_and_briefs.py`, `tests/test_principal_remediation.py` |
| OpenRouter injection and malformed decisions | `tests/test_openrouter.py` |
| Brief synthesis quote validation, privacy filtering and fallback | `tests/test_brief_synthesis.py` |
| Independent analysis stages and missing context | `tests/test_analysis_pipeline.py`, `tests/test_intelligence_and_briefs.py` |
| Public research provider, provenance and untrusted output | `tests/test_research_provider.py` |
| Competitor role false positives, structured profile and domain duplicate checks | `tests/test_company_interview_discovery.py` |
| Streamlit untrusted source-title/reason rendering | `tests/test_streamlit_app.py` |
| Source relevance and upsert/history | `tests/test_principal_remediation.py` |
| Structured CSV/XLSX quality and sample limitations | `tests/test_internal_analysis.py` |
| Verification workflow and independent corroboration | `tests/test_verification.py` |
| Schema preservation and version | `tests/test_migrations.py` |
| Actual acceptance report uses database state | `tests/test_demo_acceptance_report.py` |

## Acceptance gate

The security and local correctness tests do not prove successful company research. The MeetUniversity demo remains blocked on provider credentials/network access, source validation and human approval, real internal business records, and enough retained evidence to generate three substantive reports. No target count is inferred from candidate sources or tests.
