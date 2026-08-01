# Handoff — Prompt 2 (Alerting) complete

Date: 2026-07-22
Workstream: monitor

## What was built

Prompt 2 of 6 (ALERTING) for `fomo-fund-monitor`. The `alerting/` subsystem,
built exactly per `prompt-2-plan-v3.md` (in the multi-prompt-build tmp dir):

- `alerting/env.py` — send-time env resolution: `EmailCredentials`,
  `SmsCredentials`, `resolve_email_credentials`/`resolve_sms_credentials`/
  `resolve_recipient`. Strips values; whitespace-only ⇒ missing; all-missing
  collected in requested order into ONE `AlertError`. Never reads env at import.
- `alerting/email_alert.py` — `EmailSender` Protocol + `GmailSender`
  (`SMTP_SSL(..., timeout=SMTP_TIMEOUT_SECONDS)`, `EmailMessage` native CR/LF
  header-injection guard; wraps `SMTPException`/`OSError`/`ValueError` →
  `AlertError`; no blanket except; no credentials in messages).
- `alerting/sms_alert.py` — `SmsSender` Protocol + `TwilioSender` with
  `ClientLike`/`MessagesLike` Protocols. `twilio` import DEFERRED into
  `_default_client_factory` (`TwilioHttpClient(timeout=...)`); import-safe broad
  catch → `AlertError` (incl. `ModuleNotFoundError` → "twilio not installed").
  `body[:SMS_MAX_LENGTH]` last-resort hard cap.
- `alerting/formatting.py` — pure presentation: `SUBJECT_PREFIX_BY_EVENT` +
  per-EventType subject/body formatter tables (exact key-set = `set(EventType)`),
  `build_alert(event, config) -> Alert`, `sms_body(event) -> str` (URL-preserving
  truncation ladder). Deterministic UTC `published` render; snippet cap. Payload-
  key contract in the module docstring.
- `alerting/dispatch.py` — `DispatchResult` (frozen) + `Dispatcher`.
  `dispatch_event`/`dispatch_events` never raise; canonical EMAIL→SMS order;
  per-channel independent try/except; formatting failure → `event_error`;
  disabled sender (`None`) silently dropped.

Modified: `constants.py` (alerting env-var NAMEs + `SMS_TRUNCATION_ELLIPSIS`,
`EMAIL_SNIPPET_MAX_LENGTH=2000`, `SMTP_TIMEOUT_SECONDS=30`,
`TWILIO_HTTP_TIMEOUT_SECONDS=30`; reused `SMS_MAX_LENGTH`/`GMAIL_SMTP_*`);
`errors.py` (`AlertError`); `alerting/__init__.py` (re-export `Dispatcher`,
`DispatchResult`, `build_alert`, `AlertError` — NOT the senders). Frozen models
untouched.

Tests: `tests/test_env.py`, `test_email_alert.py`, `test_sms_alert.py`,
`test_formatting.py`, `test_dispatch.py` — typed fakes, client-method-level
mocking, no network.

## Current state

- **Python 3.11**, venv at `.venv`.
- `.venv/bin/mypy` (bare, config-driven strict): **Success — no issues in 26
  source files**.
- `.venv/bin/pytest`: **137 passed** (72 Prompt 1 + 65 new).
- `import alerting[.dispatch|.email_alert|.sms_alert|.formatting|.env]` all
  resolve; `twilio` NOT loaded by `import alerting` or by a fake-factory
  `TwilioSender.send` (deferred import confirmed).
- NOT committed (operator commits).

## Deviations / notes

- Twilio deferred import required TWO `# type: ignore[import-untyped]` (both
  `twilio.rest.Client` and `twilio.http.http_client.TwilioHttpClient` are
  untyped) plus one `cast("ClientLike", client)` for `no-any-return` — the plan's
  "at most one ignore" underestimated twilio's untyped surface. Minimal strict-
  clean form; not semantic.
- Dispatch inlines the EMAIL/SMS branches (rather than one `sender` var) to keep
  mypy strict clean without narrowing hacks — behavior identical to plan flow.
- Full detail + payload-key contract in `implementation-notes.md` (Prompt 2
  entry) and `prompt-2-result.md`.

## Next step: Prompt 3 — EDGAR monitor

Build the SEC EDGAR filing monitor under `monitors/`. It produces
`DetectedEvent`s the alerting layer consumes. Populate `payload` per the contract:
for filings, keys `filing_type`, `period`, `note` (all `dict[str,str]`; stringify
at the boundary). Set `event_type` via `constants.FILING_TYPE_EVENT.get(...,
FILING_OTHER)`, `priority` via `FILING_TYPE_PRIORITY.get(..., Priority.LOW)`,
`identifier` = accession number (dedupe key), `published` = tz-aware filing date
(the anchor), `source` = entity/"SEC EDGAR", `url` = human-readable filing link.
Use `StateStore` (`seen_filings.json`) for dedupe and enforce the first-run
lookback limit (FLAG-FR-1 in implementation-notes) to avoid an alert storm.

Do NOT scrape EDGAR HTML — use the structured JSON submissions API
(`constants.EDGAR_SUBMISSIONS_URL`) with the `USER_AGENT` header and 10 req/sec
limit. Keep new code + tests fully type-annotated (mypy strict). Run
`.venv/bin/mypy` and `.venv/bin/pytest`; both must stay green.

## Context to load

- `docs/specs/monitoring_system_spec.md` (spec)
- `workstreams/monitor.md` + `workstreams/monitor-decisions.md`
- `implementation-notes.md` (Prompt 1 + Prompt 2 entries; payload-key contract,
  FLAG-FR-1 first-run storm)
- Alerting public API: `from alerting import Dispatcher, DispatchResult,
  build_alert, AlertError`; senders from `alerting.email_alert`/`alerting.sms_alert`.
