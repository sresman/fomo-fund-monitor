# Spec Deviations & Operator Flags — fomo-fund-monitor

Consolidated audit trail from the 6-prompt multi-prompt-build (each item was reviewed by a 3-model QA panel and triaged). Reconstructed into the repo after the `/tmp` build scratch was reaped during a multi-day gap; per-prompt detail also lives in `handoffs/2026-*`.

## Deviations from `docs/specs/monitoring_system_spec.md`

### Prompt 1 — Foundation
- **SD-P1-1** — `seen_appearances.json` `conference_hashes` values are `{hash, text}` objects, not the spec's bare SHA string (the spec itself asks to store page text for diffing; greenfield, no back-compat).
- **SD-P1-2** — Atreides `filing_types` uses the real EDGAR form code `NPORT-P` where the spec table said `NPORT`.

### Prompt 2 — Alerting
- **SD-P2-1** — `alerting/` structured beyond the spec's 2-file sketch (injectable sender classes behind Protocols; env resolved at send time, not import; split into `env.py`/`formatting.py`/`dispatch.py`). Same SMTP/Twilio calls, env vars, and routing — required by the "mock at client-method level / don't read env at import / modular" conventions.

### Prompt 3 — EDGAR monitor
- **SD-P3-1** — Parse `filings.recent` PARALLEL ARRAYS (the real data.sec.gov shape), not the spec's `recentFilings` list of objects.
- **SD-P3-2** — Per-accession Archives `-index.htm` filing link (repurposed `EDGAR_FILING_INDEX_URL`) instead of the spec's browse-edgar company page.
- **SD-P3-3** — 13F XML top-position diff NOT implemented (spec stretch goal); `note` payload points to the link.
- **SD-P3-4** — Paginated backlog `filings.files[]` not fetched; v1 uses `filings.recent` (~1000 most recent). Known limitation, sufficient at 15-min cadence.
- **SD-P3-5** — First run for an entity SEEDS ALL current `recent` accessions (high-water mark) and emits ZERO alerts, so later `filing_types` expansion only alerts on net-new filings.

### Prompt 4 — Feed monitors (YouTube / Podcast RSS / Google News)
- **SD-P4-1** — `state_manager.SeenAppearances` gained a typed `markers: dict[str,str]` field (additive, backward-compatible) — the clean root-cause mechanism for per-source first-run seeding + the daily YouTube sweep marker (replaced a rejected in-bucket-sentinel hack).
- **SD-P4-2** — FEED first-run seeding is PER-SOURCE (per entity / per feed url / per query) and OBSERVATION-based: a successful fetch with zero matches still completes first-run; only a fetch failure keeps a source first-run. YouTube seeding is per-QUERY.
- **SD-P4-3** — YouTube `payload["duration"] = ""` (deferred; avoids a 2nd `videos.list` quota call).
- **SD-P4-4** — YouTube confidence gate = person SURNAME in the video title; HIGH = surname + (known_channel OR interview-framing), MEDIUM = surname only, EXCLUDE = surname absent.
- **SD-P4-5** — `youtube.known_channels` + `youtube.framing_keywords` are OPTIONAL config keys (default `()`).
- **SD-P4-6** — RSS FeedClient has no retry loop (RSS is cheap + re-polled).
- **SD-P4-7** — `seen_appearances["urls"]` bucket broadened to "web-appearance identifiers (URL or GUID)", shared by google_news (guid-first dedupe) + CNBC.

### Prompt 5 — Scrape/diff monitors (CNBC / conference pages / website diff)
- **SD-P5-1** — HTML scraping used for CNBC / conference / website (EDGAR-only prohibition; scoped exception).
- **SD-P5-2** — Content hash over NORMALIZED, TAG-STRIPPED (script/style/template/svg/head decomposed; `<noscript>` KEPT), LINE-PRESERVED extracted text — not raw HTML. Baseline guarded by min-length + a WAF challenge-phrase blocklist. Content-hash event id = `{namespaced_key}@{hash[:12]}` (unique per change).
- **SD-P5-3** — Content-hash monitors use Option A (persist-then-emit: write the new snapshot on detection, emit only if the write succeeds), NOT EDGAR/feeds Option B. Accepted tradeoff: an at-most-once content-diff alert (a dispatch failure is not retried; next change re-alerts).
- **SD-P5-4** — `season_months` is informational in v1 (all conference pages checked every run; in/out-season logged at DEBUG).
- **SD-P5-5** — For `check_rss=True` sites RSS is the PRIMARY signal; the page-hash diff is skipped (avoids double-alerting a new post). Limitation: static-page changes not in the feed are missed (**FLAG-RSS-PRIMARY**).
- **SD-P5-6** — `conference_hashes` keys namespaced (`conference:{key}` / `website:{key}`); shared content-hash store for both monitors.
- **SD-P5-7** — Keyword gating matches the diff's TRULY-added AND TRULY-removed lines (moves excluded; removals included so a cancellation alerts).
- **SD-P5-8** — RSS/CNBC first-run seeding requires a VALID observation (RSS: feedparser-recognized feed; CNBC: ≥1 result or recognizable search structure) — a WAF/JS-shell response does not seed.

### Prompt 6 — Orchestrator + Actions + bridge
- **D-ORCH-1** — `main.py` does NOT commit state to git; the GitHub Actions workflow does (keeps `main.py` pure/testable; matches the spec's own workflow YAML).
- **D-ORCH-2** — The `repository_dispatch` bridge to celeb-pm is a NET-NEW capability (optional, default-disabled, fail-soft). Sending side only; celeb-pm's receiving workflow is a SEPARATE task.
- **D-ORCH-3** — Exit 0 on per-monitor/send/bridge failures (unattended-cron hygiene); exit 2 only for a fatal config-load or startup state-probe error.
- **D-ORCH-4** — Poison events (alert-formatting crash → `DispatchResult.event_error`) are COMMITTED/quarantined (marked seen + ERROR-logged) so they never re-alert forever; the bridge carries a `local_alert_error` field. Resolves **FLAG-POISON-1**.
- **D-ORCH-5** — Bridge fires per COMMITTED event, only AFTER the mark-seen commit succeeds; at-most-once/best-effort (committed-but-bridge-failed or committed-but-push-failed events are not replayed).
- **D-ORCH-6** — `--dry-run` isolates state via a temp copy of `state/` (Option-A monitors self-persist during check, so a naive dry-run would mutate real snapshots); dry-run also force-skips all commits + bridge.
- **D-ORCH-7** — `client_payload` is nested (`{schema_version, event:{...}}`, 2 top-level keys) to respect GitHub's repository_dispatch 10-top-level-property limit.
- **D-ORCH-8** — The workflow pulls `state/` current (`git pull --rebase --autostash`) BEFORE running (prevents a queued run's stale checkout from re-alerting) and commits state only on a clean exit-0 run (`if: success()`).
- **SD-ORCH-1** — Option-A content-hash events (CONFERENCE_CHANGE, page-hash WEBSITE_DIFF) have AT-MOST-ONCE alert delivery: a transient dispatch failure is not retried (the snapshot already advanced). Logged loudly; Option-B for content-hash was rejected (no per-item id, poison-loop risk).

## Cross-cutting operator FLAGs
- **FLAG-FR-1 (first-run storm)** — RESOLVED. Every monitor seeds its backlog silently on first run (no alert flood). EDGAR seeds per-entity; feed/scrape monitors seed per-source, observation-based.
- **FLAG-POISON-1 (poison / disabled-channel infinite re-alert)** — RESOLVED by the orchestrator's 5-case mark-seen policy (poison → quarantine-commit + ERROR; all-channels-disabled → treat delivered).
- **FLAG-CNBC-JS** — OPEN (needs live validation). CNBC search is likely a JS-rendered SPA / bot-protected; requests+bs4 on raw HTML may return zero results. The parser + structural sentinel + drop-query URL canonicalization are best-guesses; validate against a live cnbc.com search page (or use CNBC's backing JSON endpoint) and confirm the video id is path- vs query-param-based before relying on CNBC alerts. Degrades gracefully to zero results.
- **FLAG-RSS-PRIMARY** — By design (SD-P5-5). For situational-awareness.com, only the Substack `/feed` is watched (new posts); static-page changes are not detected. Confirm the real feed URL resolves.

## Known limitations (accepted for v1)
- Instance-local (not process-global) rate limiting; single-process/sequential assumption (the Actions `concurrency:` guard enforces it).
- No file-locking on state (atomic `os.replace` prevents torn writes; the workflow serializes runs).
- Content-hash monitors: at-most-once alert delivery (SD-ORCH-1); full-page hashing can still be noisy on very dynamic pages (script/style/head stripped to mitigate).
- Bridge: at-most-once/best-effort; nested payload; disabled by default until celeb-pm's receiver exists.
