# Signal Stack screen-recording walkthrough

This is a recording script, not a recording. Record the full research-to-brief sequence only after the target company has real validated sources and evidence-backed signals. The current local database has no approved sources or signals, so the report will correctly show a coverage gap.

## 7–10 minute walkthrough

1. **Startup and sign-in (0:00–0:45).** Start the local app with `python scripts/run_streamlit.py`, open the local dashboard, and sign in if a password is configured. Explain the production wrapper, SQLite store, and separate scheduler process.
2. **Create or select the company (0:45–1:30).** In Company Setup, select MeetUniversity or create a company using its actual official website. Show deletion requires typing the exact company name. Never enter a fictional company for the acceptance demo.
3. **Initial research (1:30–2:30).** Run homepage research and show the exact first-party URL/excerpt. If public search is configured, use Company Intelligence Profile → “Search for public research sources.” Explain that only source-stated profile fields are sent, and the result snippets are unverified.
4. **Source discovery and review (2:30–3:30).** Show candidate category, relevance basis, reliability, discovery query/provenance, and health state. Validate the original public URL, check access/robots behavior, inspect the page, and approve only a relevant candidate. Do not count candidates as approved sources.
5. **Competitor discovery (3:30–4:30).** Search for competitor-comparison sources, review and approve suitable sources, crawl them, then run “Discover candidates from collected evidence.” Show the direct source excerpt, type, discovery date and supporting URL. Review or reject each candidate; fill product, market, segment or geography only when a cited source supports it. Never fill the 5–10 target with unsupported names.
6. **Collection and chronological feed (4:30–5:30).** Crawl approved sources and show original source, title, publication/discovery dates, excerpt, company context, ranking dimensions, and newest-first ordering. Explain that search rank and scoring are not verification.
7. **Evidence verification (5:30–6:15).** Open a signal and demonstrate the not-reviewed/source-reviewed/corroborated/disputed workflow. Independent corroboration requires a distinct source/domain; disputes require an explanation.
8. **Internal business data (6:15–7:15).** Upload an authorized, anonymized CSV/XLSX sales/support export. Show mapped columns, time period, sample size, themes, outcomes, duplicates and date limitations. State that uploaded rows stay local and do not go to LLM/search providers.
9. **Monday brief (7:15–8:45).** Generate the report from stored evidence and inspect all ten required sections and Source Appendix. Show evidence, dates, verification, uncertainty and an action owner. If there is no meaningful evidence in a category, leave it explicitly unassessed rather than inventing an event.
10. **Automation and actual counts (8:45–9:30).** Show the configured refresh cadence, scheduler job history, failures and retry state. End by opening `docs/DEMO_ACCEPTANCE_REPORT.md` and reading the actual approved-source, competitor, signal and substantive-report counts.

## Recording gate

Do not edit out source failures, substitute test fixtures for live records, or claim continuous monitoring while the local scheduler is stopped. Do not present founder-provided or search-snippet text as independently verified company fact. The repository currently contains no recorded video.
