# Monitor Workstream

> Living doc. Clean up when major blocks complete. `/resume monitor` reads this.

---

## Current State (as of Sep 3 2026) — ALERTING FIXED + NOISE POLICY

**Alerting had never delivered a single alert** (not "broke on Aug 14" — broken
from the first post-seed run, 2026-08-10T00:01:11Z). Gmail secrets were unset,
and three layers hid it: the sender wrapped every exception in a constant
string, the dispatcher stored the reason in `DispatchResult.errors`, and
`main.py` read that map for TRUTHINESS ONLY and returned 0. Every run reported
success for 25 days. Separately, EDGAR spells the Schedule 13D/G forms
`"SCHEDULE 13G"` while config used `"SC 13G"`, so **every 13D/13G filing had been
silently dropped** — including a 13D disclosing a $523.9M / 21.1% position.

**Both fixed and verified in production.** The 2026-09-03T22:49Z run delivered 6
EDGAR alerts and committed them; `state/seen_filings.json` changed for the first
time since the Aug 9 seed. `origin/main` = 21 commits this session. `mypy
--strict` clean; **pytest 616 passed** (from 450).

**Alert policy is now FIRST-PARTY APPEARANCES + SEC FILINGS.** Everything else
(Google News, MEDIUM YouTube, site diffs, other filings) is *silently captured* —
detected and committed to dedupe state, never emailed — and surfaced in a weekly
digest inside the heartbeat. Replaying the 20:31Z run's event mix through the new
policy: **153 events → ~5 emails.**

New capabilities: `--replay-since` (+ a `Replay Alerts` workflow so the app
password stays in GitHub), `--backfill-seeds`, a weekly heartbeat with the
silent-capture digest, `tools/build_master_manifest.py` (generates the YouTube
dedupe manifest from the celeb-pm corpus), and CI running mypy + pytest.

Full decision log: `implementation-notes.md` (SD-A1…SD-A55). Session handoff:
`handoffs/2026-09-03-monitor.md`.

---

## Current State (ALL 6 PROMPTS COMPLETE — as of Aug 1 2026)

**Prompt 6 (ORCHESTRATOR + CI + repository_dispatch bridge) complete — the
system is fully built.** `main.py` real orchestrator: `run(now, *, dry_run,
config_path, store, dispatcher, bridge, monitors) -> int` + thin `main()`
(`logging.basicConfig` inside main, tz-aware `datetime.now(timezone.utc)`,
`sys.exit(main())`). Module-level frozen `MonitorSpec` + `build_monitor_specs`
with the REAL 5-case commit routing (edgar→`mark_filing_seen`; youtube→
`"youtube"`; cnbc/google_news→`"urls"`; podcast_rss→`"rss_guids"`;
conference_pages→no-op Option A; website_diff→MIXED by event_type: LEOPOLD_POST→
`"rss_guids"`, WEBSITE_DIFF→no-op, else→WARNING). Frozen `Clients`. DI via small
Protocols (`StoreLike`/`DispatcherLike`/`DispatchResultLike`/`DispatchBridge`).
Startup state-probe (load_last_run + load_seen_appearances) → any exception →
CRITICAL + exit 2. Per-monitor isolation: `should_run` INSIDE each try/except,
`record_run` in `finally` (only if it actually ran, not in dry-run). Per-event
`retryable` (Option-B failed-alert events left un-committed to retry;
Option-A/one-shot committed anyway). Bridge fires ONLY after commit succeeds,
per committed event, gated on enabled + `pat_present()` (probed once/run) +
not-dry-run + auth-short-circuit flag; a bridge failure NEVER affects alerting,
mark-seen, record_run, or the exit code. `dry_run=True` short-circuits ALL
commits+bridge and runs monitors against a `tempfile.TemporaryDirectory` seeded
by copytree-if-real-else-empty (real `state/` untouched). Lazy sender/client
imports inside `run()`; NO import-time side effects. `dispatch_bridge.py`:
`DispatchBridge` Protocol + `RequestsDispatchBridge` (injectable session+sleep,
PAT at call time, one retry on 5xx/transport, 401/403→`DispatchBridgeAuthError`,
other-4xx/exhausted→`DispatchBridgeError`, capped PAT-free messages) +
`build_bridge_payload` (nested `{schema_version, event:{...}}`). `errors.py`
gained `DispatchBridgeError` + `DispatchBridgeAuthError`; `constants.py` the
bridge block; `config.py` a REQUIRED `dispatch_bridge` field (absent section →
inert disabled default). `.github/workflows/monitor.yml`: cron `*/15` +
workflow_dispatch, `contents: write`, concurrency (no cancel), timeout 10m,
`git pull --rebase --autostash` before run, setup-python 3.11 + pip cache, all 8
secrets + `DISPATCH_GITHUB_PAT`, `if: success()` diff-guarded state commit+push.
`mypy` strict clean (48 source files); `pytest` **450 passed** (393 prior − 2
placeholders + 59 new). Dry-run smoke exits 0, real state untouched, import
clean. All 6 prompts done. OPERATOR SETUP + FLAGs: see
`handoffs/2026-08-01-prompt-6-orchestrator.md`.

---

## Current State (as of July 22 2026)

Prompt 1 (FOUNDATION) complete. Flat repo-root skeleton built: `config.py`
(typed loader), `constants.py`, `models.py`, `state_manager.py` (StateStore +
interval gating), `errors.py`, `main.py` (placeholder), `monitors/` + `alerting/`
package markers, `config.yaml`, `state/` + `reference/` data dirs, and full test
suite.

Prompt 2 (ALERTING) complete. Built the `alerting/` subsystem: `env.py`
(send-time env resolution -> `EmailCredentials`/`SmsCredentials`/`resolve_recipient`,
strip + whitespace-as-missing, all-missing collected into one `AlertError`),
`email_alert.py` (`GmailSender` via `SMTP_SSL` + `EmailMessage` CR/LF guard),
`sms_alert.py` (`TwilioSender` with DEFERRED twilio import + `ClientLike`/
`MessagesLike` Protocols + import-safe broad catch), `formatting.py`
(`build_alert`/`sms_body`, per-EventType subject/body templates, exact-set tables,
URL-preserving SMS truncation ladder), `dispatch.py` (`Dispatcher`/`DispatchResult`,
fail-soft canonical EMAIL->SMS order, never raises). Added `AlertError` to
`errors.py` and alerting constants to `constants.py`. `mypy` (config-driven
strict) clean (26 source files); `pytest` 137 passed (72 Prompt 1 + 65 new).
Next: Prompt 3 (EDGAR monitor).

Prompt 3 (EDGAR MONITOR) complete. Built `monitors/edgar.py`: `EdgarClient`
Protocol + concrete `EdgarHttpClient` (injectable `sleep` shared by retry backoff
and rate limiting, `last_request_time` stamped in `finally`, retry only on
`EDGAR_RETRY_STATUS`+Timeout/ConnectionError, 403/404 fail immediately), typed
frozen `FilingRecord`/`SubmissionsResponse`, row-tolerant `_parse_submissions`
(mandatory `accessionNumber` spine, optional arrays default `[]`, within-response
first-wins dedupe), `_normalize_form`, filter-then-classify via `FILING_TYPE_*`
`.get` fallback, Option-B dedupe + first-run seed-ALL-accessions (empty payload →
no seed) + reload-merge single `save_seen_filings`, `DetectedEvent` construction
(`source=entity.name`, per-accession Archives `-index.htm` URL, tz-aware UTC
`published`, payload `filing_type`/`period`/`note`, `confidence=HIGH`), date-drift
circuit breaker, and `check_edgar(config, store, client, now)` with top-of-function
`now` tz validation and per-entity `except Exception` isolation. Added
`MonitorError` to `errors.py`; repurposed `EDGAR_FILING_INDEX_URL` +
retry/rate-limit constants in `constants.py`. New `tests/fixtures/edgar_config.yaml`
+ `tests/test_edgar.py` (51 tests) + `MonitorError` distinctness in
`tests/test_errors.py`. `mypy` strict clean (28 source files); `pytest` 188 passed
(137 prior + 51 new). Next: Prompt 4 (YouTube + Podcast RSS + Google News).

Prompt 4 (FEED MONITORS) complete. Built the three RSS-family monitors +
shared scaffold. Root-cause fix SD-P4-1: added `markers: dict[str, str]` to
`SeenAppearances` (additive, backward-compatible via `.get("markers", {})`, str
values validated → `StateError`, fresh `{}` default). `monitors/_common.py`:
`FeedClient`/`HttpGetter`/`ResponseLike` Protocol seams + `RequestsFeedClient`
(injected getter, NO retry loop), deferred-feedparser `parse_feed(bytes)` (never
raises on malformed → possibly empty; missing feedparser → `MonitorError`), frozen
`FeedEntry`, per-field `matches_keywords`, `excerpt`, suffix-stripping `surname_of`,
seed-key helpers, and `merge_appearances` (bucket dedup + markers new-keys-win,
preserves other buckets + conference_hashes). `monitors/manifest.py`:
`load_manifest_youtube_ids` via urlparse + host allowlist + anchored 11-char id
regex (rejects notyoutube.com / 12-char); never raises. `monitors/youtube.py`:
`YouTubeApiClient` (injected `build_fn`, build-once cached, API key resolved INSIDE
client from `YOUTUBE_API_KEY`, deferred googleapiclient), typed service-chain
Protocols + `VideoResult`, `check_youtube` with PER-QUERY observation-based seeding
+ broad-every-run / sweep-once-per-UTC-day (`markers["youtube_sweep"]`) + surname
confidence gate (HIGH/MEDIUM/EXCLUDE) + unified in-run `handled` dedupe + published
tz-guard + first-run-seeds-manifest-ids. `monitors/podcast_rss.py`
(`check_podcast_rss`, per-feed-url seeding, empty-url skip, best-effort person map,
source=feed.show) and `monitors/google_news.py` (`check_google_news`, per-query
seeding, guid-first dedupe in shared `urls` bucket). All Option-B: monitors seed
first-runs + youtube sweep marker but never mark normal events seen (orchestrator
does after dispatch). constants.py: YouTube API strings, host allowlist, excerpt
cap, seed-key prefixes + sweep marker. config.py + config.yaml + fixtures: optional
`youtube.known_channels`/`framing_keywords` (`_as_optional_str_tuple`, allow-empty,
reject null/scalar/empty-item/non-str). New tests (`test_monitors_common`,
`test_manifest`, `test_youtube`, `test_podcast_rss`, `test_google_news`,
state_manager markers, config optional-list) + fixtures
(`feeds_config.yaml`, `sample_podcast_feed.xml`, `sample_google_news_feed.xml`,
`master_manifest_sample.json`). Copied the real manifest to
`reference/master_manifest_v2.json` (zero YT urls, fine). `mypy` strict clean
(38 source files); `pytest` 310 passed (188 prior + 122 new). Deferred imports
verified (importing the modules pulls neither feedparser nor googleapiclient).
Next: Prompt 5 (CNBC + conference pages + website diff monitors).

Prompt 5 (SCRAPE/DIFF MONITORS) complete. Built the three scrape/diff monitors +
a new content-hash core. `monitors/_content_hash.py` (NEW, bs4/difflib/hashlib
kept out of `_common`): `extract_normalized_text` (decompose
script/style/template/svg/head, KEEP `<noscript>`, `get_text("\n")`, per-line
strip + drop-blank + rejoin), `content_hash` (sha256 over utf-8, inlined),
`make_diff` (line-oriented unified diff, changed-lines-first, cap INCLUDES the
`…(truncated)` marker), `changed_lines` (added-not-in-old ∪ removed-not-in-new,
moved-line-excluded), `is_suspect_content` (min-length + WAF phrase blocklist).
`monitors/cnbc.py`: `CnbcClient` Protocol + `CnbcHttpClient` (bs4 isolated in
`_parse_search_html` w/ structural sentinel; relative-anchor urljoin;
resolve-then-canonicalize dedupe id dropping query+fragment), per-query seeding
w/ first-run structural guard + zero-after-nonzero WARNING, `_map_query` surname
substring, Option-B (feed_events). `monitors/conference_pages.py`: content-hash
Option-A, namespaced `conference:<key>`, truly-changed-line keyword gate,
persist-then-emit (content events suppressed on save failure), season_months at
DEBUG. `monitors/website_diff.py`: ONE `feed_client` param, `_SITE_EVENT_TYPE`
code map, RSS PRIMARY when check_rss (valid-feed gate via feedparser `version`;
LEOPOLD_POST only, else WARN+skip; Option-B), page-hash else (Option-A
WEBSITE_DIFF, empty-keywords=alert-any). Extended `_common.merge_appearances`
to MERGE a `conference_hashes` update dict (verified it previously only
preserved); added seed-key + snapshot-key helpers. constants.py: CNBC urls +
labels, DIFF_SNIPPET_MAX=1500, CONTENT_MIN_TEXT_LEN=50, WAF_CHALLENGE_PHRASES,
two seed prefixes (dropped CONTENT_HASH_ENCODING). requirements-dev.txt:
types-beautifulsoup4. New tests: `test_content_hash` (16), `test_cnbc` (24),
`test_conference_pages` (19), `test_website_diff` (23), state_manager merge (1);
added a `scrape_config` conftest fixture (reuses sample_config.yaml -- has both
website_diff sites + both entities). `mypy` strict clean (46 source files);
`pytest` 393 passed (310 prior + 83 new). feedparser stays deferred; bs4 is
top-level. FLAG-CNBC-JS: the CNBC selectors + drop-query canonicalization are a
best-guess needing LIVE validation. Next: Prompt 6 (orchestrator main.py +
GitHub Actions + repository_dispatch bridge to celeb-pm).

### Session addendum — 2026-08-10 (dev-infra + triage, no monitor-feature change)
- **Config + dev-infra:** fixed a broken `known_channels` YAML indent in `config.yaml`
  (file wasn't parsing) and populated YouTube known-channels + podcast RSS URLs (`e9233e2`).
  Added `python-dotenv` + import-time `load_dotenv()` in `main.py`, `script/setup.sh`, and
  confirmed `state/*.json` are **committed on purpose** (cron pushes dedupe state back — do
  NOT gitignore) (`cf58a27`). Rebuilt the local `.venv` clean (was 3.11/3.14 split-brain).
- **YouTube triage (fed celeb-pm):** classified 25 candidate video IDs (FULL/CLIP/COMMENTARY/
  LEOPOLD). Operator confirmed 2 genuine new Baker appearances (`NGsi2PC4y68`, `MmNWwIYFBeI`)
  → processed through the celeb-pm transcripts pipeline (v9). Triage output was conversational
  (not persisted as a repo artifact).
- All fomo changes committed + pushed to `main`.

---

## Active specs in use

<!--
Specs currently being implemented in this workstream. The /resume
command reads this section and loads referenced specs into context.
Remove entries when work completes (prompted by /wrap-up). Keep
this list lean.

Format: `path/to/spec.md` -- brief context on what's being worked on.
-->

_No active specs._ — `docs/specs/monitoring_system_spec.md` is fully
implemented (all 6 prompts). The stale "continuing with Prompt 2" entry that sat
here since July was cleared on 2026-09-03; it had been flagged in the Aug 10
handoff. Post-spec work is logged in `implementation-notes.md`, not in a spec.

---

## Immediate Next Steps

1. **Operator: flip repo visibility to private.** Not doable from a session —
   the available `gh` token is `sresman-pr` with `{"admin": false}`. Cron is
   unaffected; the cron was pre-emptively halved to `*/30` because private repos
   bill Actions minutes.
2. **Confirm the Dwarkesh 403 is gone** on the next scheduled run — the fix
   (`67a0006`) landed after the last run that logged it.
3. **Watch one weekly heartbeat land** (Mondays 13:00 UTC) and confirm the digest
   renders AND drains. The drain path (`contents: write` + commit step) has not
   yet executed in production.
4. Decide on the Aleph inconsistency: the 2026-01-14 clip retrospective is
   excluded by the first-party gate, the 2026-08-12 compilation is kept. Both are
   compilations.

---

## Settled Decisions

Full details in `workstreams/monitor-decisions.md` (loaded on demand, not at startup).

**Key rules (always apply):**
1. **Never swallow a delivery failure.** Senders name the underlying exception
   CLASS and carry a redacted message; the orchestrator logs every reason and
   `run()` raises `AlertDeliveryError` at the END of the pass so per-monitor
   isolation survives but the job still exits non-zero.
2. **"Not configured" ≠ "failed."** A routed channel with no credentials is
   SKIPPED and does not block the dedupe commit. A configured channel that fails
   does block it, so the event re-fires.
3. **`alert_routing: []` is a silent capture, not a mistake** — detect, commit,
   never alert, surface in the weekly digest. `DispatchResult.routed`
   distinguishes it from "routed and nothing delivered", which is an outage.
4. **`last_run` advances only for a run that OBSERVED its sources.** A monitor
   whose source units all fail raises (`monitors/_outcome.py`).
5. **First-party is the alert bar.** YouTube HIGH requires a known publisher
   channel; podcast_rss requires the name in the title or near guest framing.
   Titles are written by uploaders and prove nothing on their own.
6. **Measure blast radius against real feed data before tightening a filter.**
   This caught two of my own bugs in one session — a stem list missing bare
   "join" (would have dropped 4 genuine appearances) and a backfill fetching
   `site.url` instead of `<site.url>/feed`.
7. **Config holds investor-specific values; code holds generic mechanism.** Full
   names live in `config.yaml`; URL-stripping and token matching live in
   `matches_keywords`.

---

## Key Files

_Add important file paths and descriptions as the workstream develops._
