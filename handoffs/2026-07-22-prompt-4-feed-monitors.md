# Handoff — Prompt 4 (FEED Monitors: YouTube + Podcast RSS + Google News) complete

Date: 2026-07-22
Workstream: monitor

## What was built

Prompt 4 of 6 for `fomo-fund-monitor`: the three FEED monitors sharing a
fetch→observe→match→dedupe→(per-source first-run seed)→emit pattern, a shared
helper module, an additive edit to `state_manager.py`, constants/config
additions, and full test coverage. Monitors return `list[DetectedEvent]`; they do
NOT send alerts (Prompt 6).

- **`state_manager.py` (SD-P4-1)**: added `markers: dict[str, str]` to
  `SeenAppearances` (seeding/scheduling metadata; NOT appearance ids). Backward-
  compatible load via `.get("markers", {})` (old files → `{}`), `_as_str_str_dict`
  validates a dict of str values → `StateError`, serialized as a copy, fresh `{}`
  default per read. No new StateStore methods (monitors read/write `app.markers`
  directly).
- **`monitors/_common.py`**: `FeedClient`/`HttpGetter`/`ResponseLike` Protocol
  seams; `RequestsFeedClient` (injected getter over `_RequestsAdapter`, NO retry
  loop, transport faults → `MonitorError`); deferred-feedparser
  `parse_feed(bytes) -> tuple[FeedEntry, ...]` (lenient — never raises on
  malformed → possibly empty; missing feedparser → `MonitorError`); frozen
  `FeedEntry`; per-field `matches_keywords`; `excerpt`; suffix-stripping
  `surname_of`; per-source seed-key helpers; `merge_appearances` (bucket dedup
  order-preserving + markers new-keys-win; preserves other buckets +
  conference_hashes). No feedparser/googleapiclient at module top.
- **`monitors/manifest.py`**: `load_manifest_youtube_ids(path) -> set[str]` via
  urlparse + host allowlist + anchored 11-char id regex (rejects
  `notyoutube.com` and 12-char runs); never raises (missing/malformed → `set()`
  + WARNING). Real manifest currently has zero YT urls → `set()`.
- **`monitors/youtube.py`**: `YouTubeApiClient` (injected `build_fn`, build-once
  cached, API key resolved INSIDE the client from `YOUTUBE_API_KEY`, deferred
  googleapiclient); typed `RequestLike`/`SearchResourceLike`/`YouTubeServiceLike`
  Protocols + `VideoResult`; `check_youtube` with PER-QUERY observation-based
  seeding, broad-every-run + sweep-once/UTC-day (`markers["youtube_sweep"]`),
  surname-in-title confidence gate (HIGH/MEDIUM/EXCLUDE), unified in-run `handled`
  dedupe vs bucket + manifest, published UTC-guard (naive→None), first-run seeds
  manifest-matched ids too.
- **`monitors/podcast_rss.py`**: `check_podcast_rss` (per-feed-url seeding,
  empty-url skip, per-field keyword match, best-effort person map, source =
  `feed.show`).
- **`monitors/google_news.py`**: `check_google_news` (per-query seeding, RSS URL
  via `quote_plus`, guid-first→link dedupe in the shared `urls` bucket).
- **`constants.py`**: `ENV_YOUTUBE_API_KEY`, YouTube API service/version/part/
  type/order + watch URL, `YOUTUBE_MANIFEST_HOSTS`, `FEED_DESCRIPTION_EXCERPT_MAX`,
  seed-key prefixes + `MARKER_YOUTUBE_SWEEP`.
- **`config.py` + `config.yaml` + fixtures**: optional
  `youtube.known_channels` / `youtube.framing_keywords`
  (`_as_optional_str_tuple`: allow-empty, reject null/scalar/empty-item/non-str;
  unknown keys still rejected). Normalized at MATCH time.
- **Tests + fixtures**: `test_monitors_common`, `test_manifest`, `test_youtube`,
  `test_podcast_rss`, `test_google_news`, plus state_manager markers cases and
  config optional-list cases; fixtures `feeds_config.yaml`,
  `sample_podcast_feed.xml`, `sample_google_news_feed.xml`,
  `master_manifest_sample.json`; a `feeds_config` conftest fixture.
- Copied the real manifest → `reference/master_manifest_v2.json`.

## Current state

- `.venv/bin/mypy` (config-driven strict): **Success — no issues in 38 source files**.
- `.venv/bin/pytest`: **310 passed** (188 prior + 122 new).
- Deferred-import sanity check passed: importing the four monitor modules pulls
  neither `feedparser` nor `googleapiclient`.
- `models.py` (frozen Prompt-1 models) untouched. NOT committed (operator commits).

## Deviations / notes

- **YouTube key vs injected build_fn**: env key required only on the DEFAULT
  (real) build path; the injected `build_fn` test seam needs no env. Missing key
  (default path) → `MonitorError("YOUTUBE_API_KEY not set")`; missing
  googleapiclient → `MonitorError("google-api-python-client not installed")`.
- `ResponseLike.content` is a read-only `@property` (so `requests.Response` fits
  under mypy strict).
- feeds test-config is a yaml fixture (schema-coupling tradeoff noted).
- Full detail + SD-P4-1..7 confirmations + FLAG-FR-1/FLAG-POISON-1 in
  `/tmp/multi-prompt-build-1784736124/implementation-notes.md`.

## Orchestrator contract (Prompt 6)

```python
yt_events   = check_youtube(config, store, YouTubeApiClient(), now)
pod_events  = check_podcast_rss(config, store, RequestsFeedClient(), now)
news_events = check_google_news(config, store, RequestsFeedClient(), now)
for ev in yt_events + pod_events + news_events:
    result = dispatcher.dispatch_event(ev, config)
    if result.event_error is None and result.channels_sent:
        store.mark_appearance_seen(kind, ev.identifier)  # kind per monitor below
```

- Monitors seed first-runs + write the youtube sweep marker themselves but do NOT
  mark normal events seen (Option B). Orchestrator marks-seen after dispatch.
- `kind`: youtube → `"youtube"`, podcast → `"rss_guids"`, google_news → `"urls"`.
- `now` must be tz-aware (validated first → `ValueError`).
- **Payload keys emitted**: YouTube → `person`/`duration`(="")/`description`;
  Podcast → `person`/`audio_url`/`description`; Google News → `query` (no person;
  `entity_key=""`).
- **FLAG-FR-1**: first-run correctness depends only on per-source successful
  observation; a first-run source returns zero events by design.
- **FLAG-POISON-1**: monitors write only first-run seeds + the youtube sweep
  marker; normal events re-emit until the orchestrator marks them seen.

## Next step: Prompt 5 — CNBC + conference pages + website diff monitors

Build the remaining monitors:
- **CNBC**: consume `AppConfig.cnbc.queries`; dedupe into the shared `urls`
  bucket ("web-appearance identifiers" — SD-P4-7); reuse the `_common` feed stack
  where applicable; `EventType.CNBC_VIDEO`.
- **Conference pages**: `AppConfig.conference_pages`; season gating via
  `season_months`; hash-diff snapshots via `StateStore.get_conference_snapshot` /
  `set_conference_snapshot` + `ConferenceSnapshot`; `EventType.CONFERENCE_CHANGE`
  (payload `diff`).
- **Website diff**: `AppConfig.website_diff`; `EventType.WEBSITE_DIFF` (payload
  `diff`) + `EventType.LEOPOLD_POST` for the situational-awareness RSS
  (payload `excerpt`).
- Reuse established seams: Protocol + concrete client, `MonitorError`, `now`
  injected + tz-validated, per-source `except Exception` isolation, Option-B
  mark-seen, deterministic order. Keep new code + tests fully type-annotated
  (mypy strict). Do NOT build the orchestrator or GitHub Actions (Prompt 6).

## Context to load

- `docs/specs/monitoring_system_spec.md` (spec)
- `monitors/_common.py` (shared feed stack + seed/merge helpers)
- `monitors/edgar.py` (client seam / narrowing / isolation precedent)
- `alerting/formatting.py` (payload-key contract per EventType)
- `state_manager.py` (`markers` field + `ConferenceSnapshot` accessors)
