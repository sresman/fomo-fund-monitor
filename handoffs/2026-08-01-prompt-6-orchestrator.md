# Handoff — Prompt 6 (ORCHESTRATOR + CI + repository_dispatch bridge) complete — WHOLE SYSTEM BUILT

Date: 2026-08-01
Workstream: monitor

## What was built (Prompt 6 — the last of 6)

The orchestrator that ties the 7 monitors + alerting + state + CI together, plus a
GitHub `repository_dispatch` bridge to the `celeb-pm` repo. With this, **all 6
prompts are complete and the system is functionally whole.**

- **`main.py`** (replaced the placeholder): the real orchestrator.
  - `run(now, *, dry_run=False, config_path=None, store=None, dispatcher=None,
    bridge=None, monitors=None) -> int` + a thin `main()` (`logging.basicConfig`
    inside `main`, tz-aware `datetime.now(timezone.utc)`, `sys.exit(main())`).
    Every collaborator is injectable for tests.
  - Module-level frozen **`MonitorSpec`** (name, bound `run_check`, per-event
    `commit`, per-event `retryable`) + frozen **`Clients`** + **`build_monitor_specs`**
    with the REAL 5-case mark-seen routing:
    1. EDGAR → `mark_filing_seen(entity_key, id)`
    2. youtube → `mark_appearance_seen("youtube", id)`
    3. cnbc / google_news → `mark_appearance_seen("urls", id)`
    4. podcast_rss → `mark_appearance_seen("rss_guids", id)`
    5. conference_pages → no-op (Option A); website_diff → MIXED by event_type
       (LEOPOLD_POST → `"rss_guids"`; WEBSITE_DIFF → no-op; any other → WARNING).
  - DI Protocols: `StoreLike`, `DispatcherLike`, `DispatchResultLike`, plus
    `DispatchBridge` (from `dispatch_bridge.py`).
  - Startup **state-probe** (`load_last_run` + `load_seen_appearances`) wrapped
    broadly → any exception → CRITICAL + **exit 2** (one clean fatal, not 7 noisy
    per-monitor failures).
  - **Per-monitor isolation**: `should_run` is INSIDE each monitor's
    `try/except Exception`; `record_run` in `finally` (only if the monitor actually
    started; never in dry-run). A `should_run` StateError skips ONLY that monitor.
  - **Per-event retryable**: Option-B events whose alert failed are left
    un-committed to re-fire next run; Option-A / one-shot events are committed
    anyway (re-firing can't help). Commit success gate = `event_error is None and
    not errors`.
  - **Bridge**: fires ONLY after a successful commit, per committed event, gated on
    `enabled` + `pat_present()` (probed once/run) + not-dry-run + an auth
    short-circuit flag (first 401/403 disables the rest of the run). A bridge
    failure NEVER affects alerting, mark-seen, record_run, or the exit code
    (fully caught + logged as WARNING). Option-A no-op commits STILL fire the bridge.
  - **dry-run**: short-circuits ALL commits + bridge + dispatch unconditionally and
    builds the `StateStore` over a `tempfile.TemporaryDirectory` seeded via
    copytree-if-real-state-exists-else-empty (real `state/` never touched);
    cleaned up in `run()`'s `finally`.
  - Lazy `GmailSender` / `TwilioSender` / concrete-client imports inside `run()`;
    **NO import-time side effects** (importing `main` pulls neither twilio nor
    feedparser). Exit 0 normally, 2 only for fatal config/state-probe. `python
    main.py` remains runnable.
- **`dispatch_bridge.py`** (NEW): `DispatchBridge` Protocol (`fire` +
  `pat_present`), `ResponseLike`/`SessionLike` seams, `RequestsDispatchBridge`
  (injectable session+sleep; PAT from `DISPATCH_GITHUB_PAT` at call time, stripped;
  POST GitHub dispatches; ONE retry w/ backoff on `requests.RequestException`/5xx;
  401/403 → `DispatchBridgeAuthError`, other-4xx/exhausted → `DispatchBridgeError`;
  capped, PAT-free messages), and `build_bridge_payload` → nested
  `{schema_version:"1", event:{event_type, entity_key, source, title, url,
  identifier, published(ISO|""), priority, confidence, monitor, detected_at,
  local_alert_error}}`.
- **`.github/workflows/monitor.yml`** (NEW): cron `*/15 * * * *` + workflow_dispatch;
  `permissions: contents: write`; concurrency group (no cancel-in-progress);
  `timeout-minutes: 10`; checkout; `git pull --rebase --autostash origin
  "$GITHUB_REF_NAME"` BEFORE run; setup-python 3.11 + pip cache; install
  requirements; run `python main.py` with all 8 secrets + `DISPATCH_GITHUB_PAT`;
  `if: success()` state-commit (git add state/ + diff-guard + commit + rebase + push).
- **`errors.py`** (partial, verified): `DispatchBridgeError` + `DispatchBridgeAuthError`.
- **`constants.py`** (partial, verified): the full bridge constants block.
- **`config.py`** (partial, verified): `dispatch_bridge` is now a REQUIRED
  `AppConfig` field; `DispatchBridgeConfig` + strict builder (absent vs present-null
  via `"dispatch_bridge" in root`; repo always str; enabled → `owner/name`
  validation; optional length-capped `event_type`); absent section → inert
  disabled default in `load_config`.
- **`config.yaml`** (partial, verified): `dispatch_bridge` section
  (`enabled: false`, placeholder `repo`, default `event_type`).
- **Tests**: replaced `tests/test_main.py` (27), created `tests/test_dispatch_bridge.py`
  (17), appended 12 dispatch_bridge cases to `tests/test_config.py`. Fixed the one
  hand-built `AppConfig(...)` site in `tests/test_youtube.py` (added the required
  `dispatch_bridge=` field).

## Current state

- `.venv/bin/mypy` (config-driven strict): **Success — no issues in 48 source files**.
- `.venv/bin/pytest`: **450 passed** (393 prior − 2 replaced placeholders + 59 new).
- Dry-run smoke: `run(now, dry_run=True)` → exit 0; real `state/` untouched
  (only `.gitkeep`); `import main` pulls neither twilio nor feedparser.
- `models.py` (frozen), the 7 monitors, `alerting/`, `state_manager.py` UNCHANGED.
- NOT committed.

## Deviations / decisions

D-ORCH-1..8 + SD-ORCH-1 (all pre-approved) — full detail in
`/tmp/multi-prompt-build-1784736124/implementation-notes.md` and
`/tmp/multi-prompt-build-1784736124/prompt-6-result.md`. Key ones:
- Commit gate is `event_error is None and not errors` (NOT `channels_sent`, which
  loops forever on a disabled-channel routing).
- Option-A / one-shot events commit even on alert failure; only Option-B failed
  alerts stay un-committed to retry.
- Bridge PAT probed once/run; first auth error short-circuits the rest.
- Bridge uses `SessionLike`/`ResponseLike` Protocol seams; the default real
  `requests.Session()` is `cast`-ed to the seam (one justified cast).

## FLAGs + resolutions

- **FLAG-CNBC-JS (open):** CNBC search selectors + drop-query canonicalization are a
  best-guess needing LIVE validation (JS/bot-protected). Degrades gracefully (zero
  results, no crash) but may silently miss CNBC hits until validated. → operator task.
- **FLAG-RSS-PRIMARY (open):** confirm `<site.url>/feed` for situational-awareness.com
  (custom-domain Substack); with `check_rss: true` the page-hash branch is skipped so
  RSS is the sole signal. → operator task.
- **FLAG-FR-1 / FLAG-POISON-1 (resolved by design):** first-run seeds all state (no
  alert flood); Option-B events re-emit until the orchestrator marks them seen after
  a successful dispatch; Option-A snapshots self-persist. The orchestrator now
  implements exactly this contract.

## OPERATOR SETUP CHECKLIST (to go live)

1. **Set GitHub Actions secrets**: `YOUTUBE_API_KEY`, `GMAIL_USER`,
   `GMAIL_APP_PASSWORD`, `TWILIO_SID`, `TWILIO_AUTH`, `TWILIO_FROM`, `ALERT_PHONE`,
   `ALERT_EMAIL` (and `DISPATCH_GITHUB_PAT` only if enabling the bridge).
2. **Fill podcast RSS feed URLs** in `config.yaml → podcast_rss.feeds[].url` (all
   currently `""`; empty ones are skipped — partial fill is fine).
3. **Validate CNBC selectors live** (FLAG-CNBC-JS): capture a real search-results
   sample, confirm `/video/` anchor selectors + structural sentinel, confirm the
   video id is in the PATH (not a query param); add a fixture.
4. **Confirm the Substack feed URL** for situational-awareness.com (FLAG-RSS-PRIMARY).
5. **Bridge (optional, currently disabled):** set `dispatch_bridge.repo`
   (`owner/name`) + flip `enabled: true` in `config.yaml`; create a PAT with
   `contents: write`/`repository_dispatch` on the receiving repo, store as
   `DISPATCH_GITHUB_PAT`.
6. **Build celeb-pm's receiving `repository_dispatch` workflow — SEPARATE TASK.** Add
   `on: repository_dispatch: { types: [fomo_monitor_event] }` in `celeb-pm` reading
   the nested `client_payload` (schema_version "1" + `event.{...}`). Keep the bridge
   disabled until this exists.
7. **Enable Actions and push.** First run seeds state (no flood); later runs alert
   only on new events; per-monitor intervals gate frequency.

## Going-live sequence (quick)

1. Secrets set → 2. RSS URLs filled → 3. CNBC/Substack feeds validated →
4. push + enable Actions (bridge still off) → 5. confirm a couple of real alert
cycles land → 6. build celeb-pm receiver → 7. set `dispatch_bridge.repo` + PAT +
`enabled: true`.

## Context to load for follow-up work

- `main.py`, `dispatch_bridge.py` (this prompt's deliverables)
- `.github/workflows/monitor.yml`
- `docs/specs/monitoring_system_spec.md`
- `alerting/dispatch.py` (dispatch contract), `state_manager.py` (state + gating)
- `/tmp/multi-prompt-build-1784736124/implementation-notes.md` (all decisions/FLAGs)
