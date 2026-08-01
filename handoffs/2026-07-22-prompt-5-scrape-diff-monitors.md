# Handoff — Prompt 5 (SCRAPE/DIFF Monitors: CNBC + conference_pages + website_diff) complete

Date: 2026-07-22
Workstream: monitor

## What was built

Prompt 5 of 6 for `fomo-fund-monitor`: the three scrape/diff monitors plus a new
content-hash core module, an extension to `_common.merge_appearances`, constants/
requirements/config-fixture additions, and full test coverage. Monitors return
`list[DetectedEvent]`; they do NOT send alerts (Prompt 6).

- **`monitors/_content_hash.py` (NEW)** — kept separate from `_common.py` so the
  feed helpers stay bs4/difflib/hashlib-free. Pure stateless helpers:
  - `extract_normalized_text(html)`: `html.parser` → decompose
    `script/style/template/svg/head` (KEEP `<noscript>` — the no-JS fallback a
    requests fetch renders) → `get_text("\n")` → per-line strip + drop-blank +
    rejoin (multi-line normalized text).
  - `content_hash(text)`: sha256 over `.encode("utf-8")` (inlined; no
    `CONTENT_HASH_ENCODING` constant).
  - `make_diff(old, new, limit)`: line-oriented `unified_diff` (lineterm=""),
    changed-lines-first, cap INCLUDES the `…(truncated)` marker (final length
    ≤ limit); identical → "".
  - `changed_lines(old, new)`: added-not-in-old ∪ removed-not-in-new (moved-but-
    identical excluded; removals ARE signal). Keyword-gate input.
  - `is_suspect_content(text)`: min-length (`CONTENT_MIN_TEXT_LEN=50`) OR
    `WAF_CHALLENGE_PHRASES` substring → skip (WAF/JS false-baseline guard).
- **`monitors/cnbc.py`** — `CnbcClient` Protocol (`search` + sentinel
  `had_recognizable_structure`) + concrete `CnbcHttpClient` (reuses the `_common`
  `HttpGetter` seam; transport fault → `MonitorError`); bs4 parse ISOLATED in
  `_parse_search_html` (best-guess `/video/` anchors; structural sentinel = a
  class token containing `searchresult`/`search-result`); relative anchors
  `urljoin`'d against `CNBC_BASE_URL`; `_canonicalize_url` = scheme + lc-netloc +
  path (query+fragment dropped, trailing slash normalized) = dedupe id; event
  `url` = resolved non-canonical url. `check_cnbc`: per-query seeding
  (`seeded:cnbc:<query>`) with first-run STRUCTURAL guard (first-run zero-result
  WITHOUT structure → not seeded), zero-after-nonzero WARNING, in-run `handled`
  set incl. seeds, `_map_query` surname substring → entity_key, Option B.
  CNBC_VIDEO priority/confidence HIGH, payload `{}`.
- **`monitors/conference_pages.py`** — `check_conference_pages` content-hash
  Option A (advance snapshot on detection), namespaced `conference:<key>`,
  WAF/min-length guard, truly-changed-line keyword gate (`page.keywords`),
  persist-then-emit (ONE batched save; content events returned only if save
  succeeded, else ERROR + suppress → re-detected next run), `season_months` at
  DEBUG (not skipped). CONFERENCE_CHANGE, source=`page.conference`, url=`page.url`,
  id=`conference:<key>@<hash12>`, published=now, LOW/MEDIUM, payload `{"diff": …}`.
- **`monitors/website_diff.py`** — `check_website_diff` with ONE `feed_client`
  param (page + `/feed`). `_SITE_EVENT_TYPE` code map (`situational_awareness_com`
  → LEOPOLD_POST; default → WEBSITE_DIFF). `check_rss=True` → RSS PRIMARY (page-
  hash skipped): `<site.url>/feed`, valid-feed gate (feedparser `version`), per-
  site seeding (`seeded:website_rss:<key>`), `rss_guids` dedupe, LEOPOLD_POST only
  (other mapping → WARN+skip), Option B, payload `{"excerpt": …}`. `check_rss=False`
  → page-hash Option A namespaced `website:<key>`, empty-keywords=alert-any-change,
  WEBSITE_DIFF payload `{"diff": …}`. Payload keyed by event_type.
- **`monitors/_common.py`** — added `cnbc_seed_key` / `website_rss_seed_key` and
  `conference_snapshot_key` / `website_snapshot_key`; **extended
  `merge_appearances`** with an optional `conference_hashes` update-dict param
  (verified: it previously only PRESERVED hashes — no update path).
- **`constants.py`** — `CNBC_BASE_URL`, `CNBC_SEARCH_URL`, `CNBC_SOURCE_LABEL`,
  `DIFF_SNIPPET_MAX=1500`, `CONTENT_MIN_TEXT_LEN=50`, `WAF_CHALLENGE_PHRASES`,
  `SEED_KEY_CNBC_PREFIX`, `SEED_KEY_WEBSITE_RSS_PREFIX`.
- **`requirements-dev.txt`** — `types-beautifulsoup4>=4.12` (installed).
- **Tests + fixture**: `test_content_hash` (16), `test_cnbc` (24),
  `test_conference_pages` (19), `test_website_diff` (23), state_manager
  conference_hashes-merge (1); `scrape_config` conftest fixture reuses
  `sample_config.yaml` (already has both website_diff sites + both entities —
  verified; no new fixture file needed).

## Current state

- `.venv/bin/mypy` (config-driven strict): **Success — no issues in 46 source files**.
- `.venv/bin/pytest`: **393 passed** (310 prior + 83 new).
- Deferred-import sanity passed: importing the three scrape monitors + the
  content-hash helpers pulls neither `feedparser`. bs4 IS top-level (hard dep).
- `models.py` (frozen) and `state_manager.py` untouched. NOT committed.

## Deviations / notes

- `merge_appearances` REQUIRED extending (previously preserve-only for
  conference_hashes) + companion state-manager merge test.
- `make_diff` also drops the empty bare `---`/`+++` file headers from the context
  section (noise with `lineterm=""` + no filenames).
- `_parse_search_html` de-dups identical resolved URLs within one parse.
- `CnbcHttpClient` lazily reuses `_common._RequestsAdapter` as its default getter.
- `is_suspect_content` is public (plan sketched `_is_suspect_content`) — unit-tested
  directly, imported by both content-hash monitors.
- Full detail + SD-P5-1..8 confirmations + all FLAGs in
  `/tmp/multi-prompt-build-1784736124/implementation-notes.md` and
  `/tmp/multi-prompt-build-1784736124/prompt-5-result.md`.

## Orchestrator contract (Prompt 6) — Option-A / Option-B split across ALL 7 monitors

```python
# Feed monitors (Option B — orchestrator marks seen after dispatch):
yt_events   = check_youtube(config, store, YouTubeApiClient(), now)          # kind "youtube"
pod_events  = check_podcast_rss(config, store, RequestsFeedClient(), now)    # kind "rss_guids"
news_events = check_google_news(config, store, RequestsFeedClient(), now)    # kind "urls"
cnbc_events = check_cnbc(config, store, CnbcHttpClient(), now)               # kind "urls"

# EDGAR (Option B, filings bucket):
edgar_events = check_edgar(config, store, EdgarHttpClient(), now)            # mark_filing_seen(entity_key, accession)

# Content-hash / mixed (Option A + Option B):
conf_events = check_conference_pages(config, store, RequestsFeedClient(), now)  # Option A — NO mark-seen
web_events  = check_website_diff(config, store, RequestsFeedClient(), now)      # page-hash Option A (no mark-seen);
                                                                               # RSS LEOPOLD_POST Option B → kind "rss_guids"

for ev in (... all events ...):
    result = dispatcher.dispatch_event(ev, config)
    if result.event_error is None and result.channels_sent:
        # Only for Option-B feed events:
        #   CNBC_VIDEO         -> mark_appearance_seen("urls", ev.identifier)
        #   LEOPOLD_POST       -> mark_appearance_seen("rss_guids", ev.identifier)
        #   (youtube/podcast/google_news as before; edgar uses mark_filing_seen)
        # CONFERENCE_CHANGE and page-hash WEBSITE_DIFF need NO mark-seen
        #   (the snapshot advance IS the dedupe; ids are unique-per-change).
        ...
```

- **Option A (self-persisting snapshots, no mark-seen):** conference_pages, and
  website_diff page-hash sites. Content-hash event ids are UNIQUE PER CHANGE
  (`{namespaced_key}@{hash[:12]}`) → normal orchestrator id-dedupe is SAFE; the
  earlier "don't dedupe content-hash by id" flag is REMOVED.
- **Option B (mark-seen after dispatch):** youtube (`youtube`), podcast_rss
  (`rss_guids`), google_news (`urls`), cnbc (`urls`), website_diff RSS
  (`rss_guids`). edgar uses `mark_filing_seen`.
- `now` must be tz-aware for every monitor (validated first → `ValueError`).
- **Payload keys**: CNBC_VIDEO `{}`; CONFERENCE_CHANGE `{"diff"}`; WEBSITE_DIFF
  `{"diff"}`; LEOPOLD_POST `{"excerpt"}` (+ Prompt-4 keys for the feed monitors).
  Prompt-2 formatters already handle all of these — no formatter changes needed.

## FLAGs to carry into Prompt 6

- **FLAG-CNBC-JS (LOUD, unresolved):** CNBC search is likely JS-rendered/bot-
  protected. `_parse_search_html` selectors + structural sentinel + drop-query
  canonicalization are a BEST-GUESS needing LIVE validation (confirm selectors;
  capture a real sample for a fixture; confirm video id lives in PATH vs QUERY
  PARAM — if query-param, canonicalization MUST change). Degrades gracefully today.
- **FLAG-RSS-PRIMARY (LOUD):** check_rss=True → RSS sole signal, page-hash skipped
  → static-only page changes missed. Confirm `<site.url>/feed` for
  situational-awareness.com (custom-domain Substack).
- **FLAG-FR-1** (carried): first-run correctness depends only on per-source
  successful observation; a first-run source emits nothing by design.
- **FLAG-POISON-1** (carried): monitors write only first-run seeds/markers +
  snapshot advances; Option-B normal events re-emit until the orchestrator marks
  them seen after a successful dispatch. Option-A content-hash events do NOT re-fire
  because the snapshot already advanced+persisted.

## Next step: Prompt 6 — orchestrator + CI

- `main.py` orchestrator: load config + StateStore, tz-aware `now`, interval-gate
  each monitor via `should_run`/`record_run`, run all 7 monitors, dispatch each
  event, apply the Option-A/Option-B mark-seen split above, record runs.
- GitHub Actions workflow (scheduled cron) + a `repository_dispatch` bridge to the
  `celeb-pm` repo.

## Context to load

- `docs/specs/monitoring_system_spec.md` (spec)
- `monitors/_common.py` + `monitors/_content_hash.py` (shared stacks)
- `monitors/edgar.py`, `monitors/google_news.py` (Option-B / isolation precedent)
- `alerting/dispatch.py` + `alerting/formatting.py` (dispatch + payload-key contract)
- `state_manager.py` (`markers`, `ConferenceSnapshot`, `mark_appearance_seen`,
  interval gating)
- `/tmp/multi-prompt-build-1784736124/implementation-notes.md` (all decisions/FLAGs)
