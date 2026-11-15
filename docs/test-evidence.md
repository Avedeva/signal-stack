# Test and demonstration evidence

## Automated checks

Final full run, 2026-11-15, Windows/Python 3.12:

```text
python -m pytest -q
121 passed, 5 skipped in 14.82s
```

The five skips are local HTTP integration cases blocked by default sandbox loopback policy. The Streamlit suite including the brief-synthesis opt-in and untrusted source-title/reason regression passed separately: `python -m pytest -q tests/test_streamlit_app.py` → **9 passed**. Focused synthesis/report/provider tests → **28 passed**. Tests verify the Decider model uses the Decisions API, prose uses a separate free chat model, paid model IDs are refused, quotes are checked, private inputs are excluded, empty windows cause no API call, and errors fall back deterministically.

The five skips are local HTTP integration cases because the default sandbox blocks loopback connections. With explicit loopback access, the crawler integration group ran against a real local HTTP server:

```text
python -m pytest -q tests/test_security_and_extraction.py -k local_http
5 passed, 11 deselected in 3.91s
```

Other final checks:

```text
python -m compileall -q src app.py tests scripts
# passed

python -m pip check
# No broken requirements found.

$env:PYTHONPATH = "src"; python -m signal_stack.cli --help
# CLI startup/help passed.
```

The focused analysis/search/UI/auth suite passed 37 tests before the final competitor-profile extension; the focused company/migration/Streamlit/deletion suite then passed 31 tests. New tests cover staged event/company-impact/action analysis, missing-context restraint, Tavily API request bounds and result validation, provider privacy, snippet provenance, no-provider UI state, competitor role false positives, structured profile editing, duplicate domains and schema upgrades. `tests/test_migrations.py` passes preservation checks through schema v17.

Coverage was requested with `python -m pytest --cov=signal_stack --cov-report=term-missing`, but this environment does not have `pytest-cov` installed; pytest rejected `--cov`. No coverage percentage or 80% claim is made. Installing optional development dependencies was not verified.

## Database migration and measured acceptance

The local database was at schema v12 while the code migration chain reached v16. Before that upgrade, a local ignored backup was created at `data/signal_stack_pre_v16_2026-11-15.db`. The later competitor-profile migration was preceded by `data/signal_stack_pre_v17_2026-11-15.db`. Additive `python -m signal_stack.cli init-db` commands completed with “existing rows were not dropped.” A subsequent query confirmed schema v17 and all 3 companies remained. The acceptance report was then refreshed through a read-only SQLite connection.

Current measured state is documented in `DEMO_ACCEPTANCE_REPORT.md`:

- MeetUniversity: 11 source candidates, 0 approved/healthy, 0 competitors, 0 documents/developments/signals, 0 substantive reports.
- Zerodha: 10 source candidates, 0 approved/healthy, 0 competitors, 0 documents/developments/signals, 0 substantive reports.
- Zomato: 8 source candidates, 0 approved/healthy, 0 competitors, 0 documents/developments/signals, 0 substantive reports.
- Across companies: six saved briefs, all zero-signal coverage-gap reports; five have all ten current headings.

The acceptance targets (20 approved MeetUniversity sources, 5–10 evidence-backed competitors, 30 real signals, and three substantive reports) remain unmet. No test fixture or search result candidate is counted toward them.

## Live operation and packaging

- No live Tavily, OpenRouter, Ollama, or company-source request was made. Tavily API behavior was tested with a fake HTTP response.
- Docker Compose YAML parses and its required services/configuration were inspected, but Docker is unavailable here; image build, container startup and deployment were not tested.
- Direct application dependencies are pinned in `pyproject.toml`; a full transitive lockfile and clean dependency installation are not verified.
- The repository has no tracked files and no Git remote. No commit, push, GitHub publication, public deployment, or screen recording is claimed.
- `.env`, database values, credentials and keys were not inspected. Example configuration contains placeholders only.
