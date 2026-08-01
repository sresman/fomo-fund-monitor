# Handoff — Prompt 3 (SEC EDGAR Monitor) complete

Date: 2026-07-22
Workstream: monitor

## What was built

Prompt 3 of 6 for `fomo-fund-monitor`: **Monitor 1, the SEC EDGAR filing
monitor**. Reads the EDGAR structured submissions JSON (never HTML), zips the
`filings.recent` parallel arrays into typed per-filing records (row-tolerant),
filters/classifies/dedupes against `StateStore`, seeds state silently on first
run, and returns `list[DetectedEvent]`. Does NOT send alerts (Prompt 6).

- `monitors/edgar.py`:
  - `EdgarClient` Protocol + concrete `EdgarHttpClient` (injectable `sleep` shared
    by retry backoff AND rate limiting; `last_request_time` stamped in `finally`;
    retry only on `EDGAR_RETRY_STATUS`/Timeout/ConnectionError; 403/404 fail
    immediately; total attempts `1 + HTTP_MAX_RETRIES`; JSON narrowed at the
    single `.json()` boundary).
  - Typed frozen `FilingRecord` / `SubmissionsResponse`.
  - `_parse_submissions(obj, requested_cik)` — mandatory `accessionNumber` spine
    (missing → `MonitorError`), optional arrays default `[]`, per-row lenient
    coercion (blank/non-str accession skips the row; other fields → `""`),
    within-response first-wins dedupe with WARNING, CIK sanitation + fallback.
  - `_normalize_form`, filter-then-classify via `FILING_TYPE_EVENT`/
    `FILING_TYPE_PRIORITY` `.get(...)` fallback.
  - Option-B dedupe + first-run seed-ALL-accessions (empty payload → no seed,
    stays first-run) + reload-merge before a single `save_seen_filings`.
  - `DetectedEvent`: `source=entity.name`, per-accession Archives `-index.htm`
    URL, tz-aware UTC `published`, payload `filing_type`/`period`/`note`,
    `confidence=HIGH`. Date-drift circuit breaker (100% date-skip on a
    non-first-run entity → ERROR).
  - `check_edgar(config, store, client, now)` — `now` tz-validated at the top
    (before state load, outside the per-entity catch); initial load fatal; final
    seed-save non-fatal (log + return); per-entity `except Exception` isolation.
- `errors.py`: `MonitorError(Exception)` (distinct; no `__all__` to update).
- `constants.py`: repurposed `EDGAR_FILING_INDEX_URL` → per-accession template;
  added `EDGAR_RETRY_BACKOFF_SECONDS`, `EDGAR_RETRY_STATUS`,
  `EDGAR_MIN_REQUEST_INTERVAL_SECONDS`.
- Tests: `tests/test_edgar.py` (50 tests, typed fake client + fake session, no
  network), `tests/fixtures/edgar_config.yaml`, `MonitorError` distinctness in
  `tests/test_errors.py`.

## Current state

- `.venv/bin/mypy` (config-driven strict): **Success — no issues in 28 source files**.
- `.venv/bin/pytest`: **188 passed** (137 prior + 51 new).
- `python -c "from monitors.edgar import check_edgar, EdgarClient, EdgarHttpClient"` → ok.
- Prompt 1 frozen models untouched. NOT committed (operator commits).

## Deviations / notes

- No true plan deviations. Retry backoff via a `range(1 + HTTP_MAX_RETRIES)` loop
  (`base * attempt` for `attempt > 0`) = plan's 1-based numbering. Status handled
  via `response.status_code` (no `raise_for_status`). Two additive tests beyond
  the matrix (bad-JSON via concrete client; a genuinely-empty `filing_types`
  config variant). One required `# type: ignore[arg-type]` at the duck-typed test
  session seam. Full detail + SD-P3-1..5 confirmations in
  `/tmp/multi-prompt-build-1784736124/implementation-notes.md`.

## Orchestrator contract (Prompt 6)

```python
events = check_edgar(config, store, EdgarHttpClient(), now)
for ev in events:
    result = dispatcher.dispatch_event(ev, config)
    if result.event_error is None and result.channels_sent:
        store.mark_filing_seen(ev.entity_key, ev.identifier)
```

- Monitor persists ONLY first-run seeds; NEVER marks normal filings seen (Option B).
- Payload keys emitted: `filing_type`, `period`, `note`.
- **FLAG-POISON-1** (Prompt 6): treat all-disabled-channels as delivered; catch
  dispatcher exceptions; quarantine/mark-seen after N failed attempts.
- **FLAG-FR-1**: a first-run entity returns zero events by design.

## Next step: Prompt 4 — YouTube + Podcast RSS + Google News feed monitors

Build the feed-based monitors. Consume:
- `AppConfig.youtube` / `.podcast_rss` / `.google_news`, and
  `StateStore.load_seen_appearances` / `mark_appearance_seen` (kinds:
  `youtube`, `rss_guids`, `urls`).
- Constants: `GOOGLE_NEWS_RSS_URL`, `YOUTUBE_*` quota constants, `USER_AGENT`,
  `HTTP_TIMEOUT_SECONDS`, `HTTP_MAX_RETRIES`.
- Reuse the EDGAR seams as precedent: Protocol + concrete client, injectable
  `sleep`, JSON/feed narrowing at the boundary, `MonitorError` for source faults,
  `now` injected + tz-validated, per-entity/per-feed `except Exception` isolation,
  deterministic order. Emit `DetectedEvent`s with the payload keys the Prompt 2
  formatters read (`person`/`duration`/`description`, `audio_url`, `query`).

Keep new code + tests fully type-annotated (mypy strict). Run `.venv/bin/mypy`
and `.venv/bin/pytest -q`; both must stay green.

## Context to load

- `docs/specs/monitoring_system_spec.md` (spec)
- `monitors/edgar.py` (precedent for client seam / narrowing / isolation)
- `alerting/formatting.py` (payload-key contract per EventType)
