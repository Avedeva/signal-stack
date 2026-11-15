# Signal Stack

Signal Stack is a local, evidence-first company intelligence and executive briefing application. It stores company context, founder interviews, source candidates and approvals, crawled documents, evidence-linked signals, briefs, actions, and scheduled-job history in SQLite.

The app uses OpenRouter's Perplexity Decider Flash V1.3 free decision model for public-signal classification. A separate free chat model, `openai/gpt-oss-120b:free`, powers the unchecked Monday-brief synthesis option because the decision model returns structured choices rather than prose. The synthesis is retained only when each item includes a quote matched to the stored source excerpt; implications remain model suggestions, not verification. With the option enabled, the request includes the selected company name, public signal excerpts, and only source-stated profile fields; internal uploads, founder interview answers, and founder-provided profile fields are excluded. Provider failures fall back to the deterministic evidence-based report. Both OpenRouter integrations reject model IDs without the `:free` suffix. If configured, local Ollama can classify evidence-backed company context and draft the founder interview. Optional Tavily Search discovers public source candidates; its snippets stay unverified until an original page is validated and crawled. Internal uploads and founder interview answers are never sent to OpenRouter or Tavily.

## Current build status

The current measured local database state is in [Demo Acceptance Report](docs/DEMO_ACCEPTANCE_REPORT.md), refreshed through a read-only SQLite query. It distinguishes candidates from approved sources, coverage-gap briefs from substantive reports, and saved interview answers from completed interviews. No claim is made that a company is being monitored continuously. The assignment targets require real, validated public evidence and have not been met in this environment.

See [test evidence](docs/test-evidence.md), [architecture and limitations](docs/architecture.md), [cost statement](docs/cost-statement.md), and the [demo script](docs/demo-script.md).
The review-to-implementation mapping is in the [Principal Remediation Checklist](docs/PRINCIPAL_REMEDIATION_CHECKLIST.md).
The requirement-by-requirement assignment audit is in the [Assignment Acceptance Matrix](docs/ASSIGNMENT_ACCEPTANCE_MATRIX.md).

## Requirements

- Python 3.11 or newer (developed with Python 3.12 on Windows 11).
- Internet access from the machine running the app to install Python dependencies and to check public sources. A blocked/sleeping host cannot collect or schedule jobs.
- Optional: an OpenRouter API key or a local Ollama installation/model. The selected OpenRouter model is listed as free, but OpenRouter still requires an account/API key and may rate limit free access.
- Optional for broader public source discovery: a Tavily Search API key. Search is disabled by default. See [Tavily Search API documentation](https://docs.tavily.com/documentation/api-reference/endpoint/search); provider access and usage terms are controlled by Tavily.
# Video


https://github.com/user-attachments/assets/a88f5e91-02e9-41d7-b225-02ae84e94607



## Setup on Windows PowerShell

From the project directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m signal_stack.cli init-db
python -m signal_stack.cli bootstrap-demo --research
python -m signal_stack.cli import-profile-evidence --path demo_evidence/company_profiles.json
```

The bootstrap command creates/reuses MeetUniversity, Zerodha, and Zomato, then attempts actual homepage research. The optional profile fixture imports cited browser-reviewed first-party facts and creates the initial interview questions; it does not create crawler records or approve a source. Candidate sources must pass a live robots/access check and be reviewed before approval. If a company was already configured, it is reused.

Start the dashboard:

```powershell
python scripts/run_streamlit.py
```

Or use `scripts\run_app.ps1` after installing dependencies. The wrapper reads local environment configuration before starting Streamlit and binds to `SIGNAL_STACK_BIND_ADDRESS` (loopback by default).

### Authentication and deployment configuration

The default `development` environment is intended for one trusted local user and requires a loopback bind address. To require a password locally, set `SIGNAL_STACK_AUTH_PASSWORD_HASH` and `SIGNAL_STACK_SESSION_TIMEOUT_MINUTES`. Generate a salted PBKDF2-SHA256 hash without putting a plain password in shell history:

```powershell
$env:PYTHONPATH = "src"
python -c "from getpass import getpass; from signal_stack.auth import hash_password; print(hash_password(getpass('New app password: ')))"
```

For a network-accessible deployment, set `SIGNAL_STACK_ENVIRONMENT=production`, configure the generated value as `SIGNAL_STACK_AUTH_PASSWORD_HASH`, and provide a unique random `SIGNAL_STACK_SESSION_SECRET` with at least 32 characters. Production startup fails closed if either is missing. The startup wrapper also passes the secret to Streamlit's cookie signing configuration. Set `SIGNAL_STACK_BIND_ADDRESS` to the chosen interface only after enabling production authentication. The account is a single founder/admin account with access to all company workspaces; this build does not implement multi-user roles or per-company membership.

Do not commit `.env`. Configure secrets through the host's secret manager. Back up the database and upload directory together. SQLite is intended for a single-node deployment; do not mount one database file into multiple independent app/scheduler writers without a database migration plan.

## Scheduler

Run the scheduler in a second terminal and keep it open:

```powershell
python -m signal_stack.cli scheduler
```

Or use `scripts\run_scheduler.ps1`. The scheduler polls persisted company schedules, runs weekly collection/brief generation and monthly source audits, records outcomes, and prevents overlapping runs. Closing the process, sleeping the machine, or stopping a host pauses automation. For a one-time run, open **Automation / Job History** or use:

```powershell
python -m signal_stack.cli weekly --company-id 1
python -m signal_stack.cli audit --company-id 1
```

On non-Windows systems, set `PYTHONPATH=src` before running `python -m signal_stack.cli ...`.

## Main workflows

1. Create or select a company in **Company Setup** and click **Run / resume onboarding**. Research failures are recorded per stage; successful stages remain saved and failed stages can be retried.
2. To remove a company, go to **Company Setup**, select it under **Delete a company**, and type its exact name. This permanently deletes that company's database records and managed uploaded files.
3. Review the source-stated profile and evidence-based industry classification, add founder context, and complete the one-question-at-a-time interview. Unanswered questions refresh when relevant profile context changes; saved answers remain intact.
4. Review source candidates; validate public candidates, inspect their reasons and relevance, then approve one or several recently validated sources together. In **Competitors**, discover candidates from collected comparison evidence, review type and cited excerpt, and leave unsupported profile fields blank.
5. Crawl approved sources manually or run the weekly workflow. Failed checks remain visible in crawl/job history.
6. Review signals with evidence, verification state, separate ranking dimensions, and source links.
7. Generate the Monday brief from stored signals. It includes Executive Summary, Customer Signals, Competitor Intelligence, Market Intelligence, Internal Business Intelligence, Opportunities Identified, Risks & Threats, Recommended Actions for This Week, What We Should Watch, and Source Appendix. Enable **Add OpenRouter synthesis from public evidence** only when you want the model to summarize public signals; internal uploads and founder answers stay local. Leading developments include why they matter, a next step, priority, a quote-matched evidence excerpt, source link, and verification status. If the evidence supports fewer than three opportunities, the brief reports the actual count instead of inventing items. Empty or incomplete source coverage is called out instead of being presented as proof that nothing happened.
8. Add CSV, XLSX, PDF, or TXT files to **Internal File Upload**. Files are parsed locally and treated as untrusted evidence.

## Useful commands

```powershell
python -m signal_stack.cli init-db
python -m signal_stack.cli bootstrap-demo --research
python -m signal_stack.cli discover --company-id 1
python -m signal_stack.cli research --company-id 1
python -m signal_stack.cli source-status --company-id 1 --source-id 1 --status approved
python -m signal_stack.cli crawl --company-id 1
python -m signal_stack.cli competitors --company-id 1
python -m signal_stack.cli weekly --company-id 1
python -m signal_stack.cli export-brief --company-id 1
python scripts/refresh_demo_acceptance_report.py
python -m pytest -q
```

The source-status command requires successful validation within the prior seven days before approving a public source. Internal upload sources are local and do not use network validation.

## Configuration

Copy `.env.example` to `.env`, set `SIGNAL_STACK_LLM_PROVIDER=openrouter`, and add your own `OPENROUTER_API_KEY` to enable public-signal classification and the explicit Monday-brief synthesis option. The decision model defaults to `perplexity/pplx-decider-flash-v1.3:free`; the separate prose model defaults to `openai/gpt-oss-120b:free`. Both model settings require a `:free` variant to prevent paid model calls. OpenRouter still requires an account/API key and may rate limit free access. Set `SIGNAL_STACK_RESEARCH_SEARCH_PROVIDER=tavily` and `SIGNAL_STACK_TAVILY_API_KEY` only when enabling public search. Search sends the company name and source-stated public profile fields; founder interview answers and uploaded records are excluded. Every result remains an unapproved source candidate. The example file contains placeholders only. Shell environment variables take precedence. Useful settings include `SIGNAL_STACK_LLM_PROVIDER` (`openrouter`, `ollama`, or `none`), `SIGNAL_STACK_OPENROUTER_MODEL`, `SIGNAL_STACK_OPENROUTER_BRIEF_MODEL`, `SIGNAL_STACK_OLLAMA_URL` (loopback only), `SIGNAL_STACK_OLLAMA_MODEL`, the SQLite database path, request limits, and default time zone. Do not commit `.env`.

The default database is `data/signal_stack.db`; local uploads are stored under `data/uploads/`. Both are ignored by Git. Back up the database and uploads together if you need to preserve a workspace. Migrations run additively during `python -m signal_stack.cli init-db`; the current schema is version 17.

## Development and tests

```powershell
python -m pytest -q
```

Tests use temporary SQLite databases and mocked HTTP responses. The suite covers URL/SSRF checks, robots behavior, RSS/HTML parsing, evidence and history, ranking, company isolation, uploads, reports, migrations, schedule execution/failure logging, and the Streamlit page render path. These tests do not prove live source availability or deployment behavior.

## Single-node Docker deployment

`Dockerfile` and `compose.yaml` provide a production-authenticated, single-node deployment with a non-root app user, additive migration step, persistent database/uploads/reports volumes, bounded logs, a health check, and a loopback-only host port (`127.0.0.1:8501`). The scheduler shares the local SQLite volume and runs separately. Do not scale these services horizontally or put the SQLite volume on a network filesystem.

Generate a password hash interactively and set a random session secret in the current PowerShell session. These process environment variables are not written into the repository:

```powershell
$env:PYTHONPATH = "src"
$env:SIGNAL_STACK_AUTH_PASSWORD_HASH = python -c "from getpass import getpass; from signal_stack.auth import hash_password; print(hash_password(getpass('New app password: ')))"
$env:SIGNAL_STACK_SESSION_SECRET = ([guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N"))
docker compose up --build -d
docker compose ps
docker compose logs -f app scheduler
```

Open `http://127.0.0.1:8501`. Compose refuses to start if either production credential is missing. Add a reverse proxy only if it preserves authentication and TLS; change the host binding deliberately for remote access. Back up `signal_stack_data` and `signal_stack_reports` together. A Docker build or deployment has not been executed in this workspace.

## Repository publication status

This directory is a local working tree. It has not been pushed to GitHub, publicly deployed, or recorded as a demo video. The application uses local SQLite and a separate scheduler process; a sleeping free host is not suitable for reliable recurring runs. See the demo script for an honest on-screen walkthrough.
