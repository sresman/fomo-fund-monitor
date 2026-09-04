from __future__ import annotations

"""Fixed, non-tunable code-level constants.

The single intra-app import direction here is ``constants -> models``;
``models`` imports nothing back. All user-tunable values live in ``config.yaml``;
this file holds only fixed code-level constants (base URLs, timeouts, rate
limits, filing-type maps, monitor-name set, state filenames).
"""

from models import EventType, MonitorName, Priority

# --- HTTP / SEC EDGAR ---

# SEC requires a descriptive User-Agent with contact info. Kept in constants
# (not config) intentionally -- it identifies this application, not a tunable.
USER_AGENT: str = "fomo-fund-monitor (steve@collectifadv.com)"

# Structured JSON only -- never HTML scraping (per project conventions).
EDGAR_SUBMISSIONS_URL: str = "https://data.sec.gov/submissions/CIK{cik}.json"

# Per-accession Archives index template for a specific filing. Placeholders:
#   {cik_int}   -- CIK with leading zeros stripped
#   {acc_nodash}-- accession number with dashes removed
#   {acc_dash}  -- accession number with dashes
# Points directly at THE filing's index page (deterministic, structured path).
EDGAR_FILING_INDEX_URL: str = (
    "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{acc_dash}-index.htm"
)

SEC_RATE_LIMIT_PER_SEC: int = 10

HTTP_TIMEOUT_SECONDS: int = 20  # default requests timeout (later monitors import)
HTTP_MAX_RETRIES: int = 3

# EDGAR retry / rate-limit tuning (Prompt 3). Backoff before retry N is
# EDGAR_RETRY_BACKOFF_SECONDS * N (1-based). Retry only on transient statuses;
# 403/404 fail immediately.
EDGAR_RETRY_BACKOFF_SECONDS: float = 1.0
EDGAR_RETRY_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
EDGAR_MIN_REQUEST_INTERVAL_SECONDS: float = 1.0 / SEC_RATE_LIMIT_PER_SEC

# --- Alerting (Prompt 2) ---

GMAIL_SMTP_HOST: str = "smtp.gmail.com"
GMAIL_SMTP_PORT: int = 465
SMS_MAX_LENGTH: int = 1600  # SMS truncation length

# Alerting env-var NAMES (resolved from os.environ at send time, never at import).
ENV_GMAIL_USER: str = "GMAIL_USER"
ENV_GMAIL_APP_PASSWORD: str = "GMAIL_APP_PASSWORD"
ENV_TWILIO_SID: str = "TWILIO_SID"
ENV_TWILIO_AUTH: str = "TWILIO_AUTH"
ENV_TWILIO_FROM: str = "TWILIO_FROM"

# Alerting presentation / transport scalars.
SMS_TRUNCATION_ELLIPSIS: str = "…"
EMAIL_SNIPPET_MAX_LENGTH: int = 2000  # cap for diff/excerpt/description in email body
SMTP_TIMEOUT_SECONDS: float = 30  # SMTP_SSL socket timeout
TWILIO_HTTP_TIMEOUT_SECONDS: float = 30  # TwilioHttpClient(timeout=...)

# Alert-failure reporting. A sender wraps its underlying exception in an
# AlertError; the wrapper message carries the exception CLASS plus a sanitized,
# capped rendering of its message so an operator can tell an auth rejection from
# a DNS failure. Credential VALUES are substituted out before the cap is applied.
REDACTED_PLACEHOLDER: str = "<redacted>"
ALERT_FAILURE_DETAIL_MAX_CHARS: int = 300
# Max per-event failure summaries embedded in the end-of-run AlertDeliveryError
# message (the full set is already in the log, one ERROR line per event).
ALERT_FAILURE_SAMPLE_MAX: int = 5

# --- YouTube (Prompt 4) ---

YOUTUBE_SEARCH_COST_UNITS: int = 100
YOUTUBE_DAILY_QUOTA: int = 10000

# YouTube env-var NAME (resolved from os.environ INSIDE the concrete client,
# NEVER at import time).
ENV_YOUTUBE_API_KEY: str = "YOUTUBE_API_KEY"

# YouTube Data API v3 search.list fixed parameters.
YOUTUBE_API_SERVICE_NAME: str = "youtube"
YOUTUBE_API_VERSION: str = "v3"
YOUTUBE_SEARCH_PART: str = "snippet"
YOUTUBE_SEARCH_TYPE: str = "video"
YOUTUBE_SEARCH_ORDER: str = "date"  # newest first
YOUTUBE_WATCH_URL: str = "https://www.youtube.com/watch?v={video_id}"

# Manifest YouTube-URL host allowlist (lowercased; urlparse().netloc must be in
# this set for an id to be extracted -- rejects notyoutube.com and embedded-path
# fakes).
YOUTUBE_MANIFEST_HOSTS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtube-nocookie.com",
        "youtube-nocookie.com",
    }
)

# --- Google News (Prompt 4) ---

GOOGLE_NEWS_RSS_URL: str = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# --- CNBC (Prompt 5) ---

# Base for urljoin() of relative /video/... anchors returned by the search page.
CNBC_BASE_URL: str = "https://www.cnbc.com"
# {query} is urllib.parse.quote_plus-encoded at the call site.
CNBC_SEARCH_URL: str = "https://www.cnbc.com/search/?query={query}&type=video"
CNBC_SOURCE_LABEL: str = "CNBC"

# --- Content-hash diff (Prompt 5) ---

# STRICTLY < EMAIL_SNIPPET_MAX_LENGTH (2000) so the email formatter never
# re-truncates mid-diff; the cap INCLUDES the truncation marker.
DIFF_SNIPPET_MAX: int = 1500
# Below this normalized-text length, treat the fetch as failed / a JS-only
# skeleton / a WAF challenge -> skip (do not seed, do not diff).
CONTENT_MIN_TEXT_LEN: int = 50
# Case-insensitive substring match on normalized text -> suspect (bot-challenge
# interstitial long enough to pass the min-length check but not real content).
WAF_CHALLENGE_PHRASES: tuple[str, ...] = (
    "checking your browser",
    "enable javascript",
    "verify you are human",
    "cloudflare",
    "attention required",
    "captcha",
)

# --- RSS / feeds (Prompt 4) ---

# payload["description"] cap at the monitor boundary (the email formatter re-caps
# at EMAIL_SNIPPET_MAX_LENGTH).
FEED_DESCRIPTION_EXCERPT_MAX: int = 500

# --- First-party appearance gate (podcast_rss) ---
#
# A configured feed is an official publisher, but an EPISODE on it may only
# MENTION the person -- a show-notes link, a cross-reference to another podcast,
# a tweet citation. Those are not appearances. The gate: the name is in the
# TITLE (whoever is named in an episode title is the guest), or it is in the
# description within APPEARANCE_FRAMING_WINDOW_CHARS of a guest-framing stem.
#
# Stems are PREFIXES matched at a word boundary, so "join" covers
# join/joins/joined/joining. That breadth is load-bearing: All-In writes
# "Gavin Baker and Travis Kalanick join the show!", and a stem list containing
# only "joins" would have suppressed four genuine Baker appearances.
APPEARANCE_FRAMING_STEMS: tuple[str, ...] = (
    "join",
    "guest",
    "interview",
    "sits down",
    "sit down",
    "sat down",
    "in conversation",
    "conversation with",
    "talks to",
    "talking to",
    "speaks with",
    "speaking with",
    "chats with",
    "welcome",
    "returns to the show",
    "on the show",
    "on the pod",
    "in studio",
    "live with",
)
# Characters either side of the matched name that are scanned for a stem.
APPEARANCE_FRAMING_WINDOW_CHARS: int = 200

# A framing stem followed by a RECIPROCAL object is a compilation tell, not a
# guest. "Michael Eisenberg ... puts them in conversation WITH ONE ANOTHER" is
# archive clips juxtaposed; "in conversation with Gavin Baker" is an appearance.
# Applied to the text immediately AFTER a matched stem, within
# APPEARANCE_RECIPROCAL_LOOKAHEAD_CHARS, so it can only veto the stem it follows.
#
# Deliberately narrow. Two broader designs were measured against every configured
# feed and rejected: dropping the "in conversation" stems outright also worked
# but gave up a real guest-framing phrase, and a general "special episode / best
# of / curates" compilation veto suppressed TWO GENUINE ILTB Baker appearances
# (2024-08-27, 2022-01-25) that are in the transcript corpus.
APPEARANCE_RECIPROCAL_OBJECTS: tuple[str, ...] = (
    "one another",
    "each other",
    "themselves",
)
APPEARANCE_RECIPROCAL_PREPOSITIONS: tuple[str, ...] = ("with", "among", "between")
APPEARANCE_RECIPROCAL_LOOKAHEAD_CHARS: int = 60

# --- Seeding / scheduling MARKER keys (live in SeenAppearances.markers) ---
# Per-source seed keys are BUILT at runtime from these prefixes (see
# monitors/_common.py seed-key helpers). They are NOT dedupe identifiers; they
# live in the `markers` dict.
SEED_KEY_YOUTUBE_PREFIX: str = "seeded:youtube:"  # + query.strip()  (PER-QUERY)
SEED_KEY_PODCAST_PREFIX: str = "seeded:podcast:"  # + feed.url.strip()
SEED_KEY_NEWS_PREFIX: str = "seeded:news:"  # + query.strip()
SEED_KEY_CNBC_PREFIX: str = "seeded:cnbc:"  # + query.strip()  (PER-QUERY, urls bucket)
SEED_KEY_WEBSITE_RSS_PREFIX: str = "seeded:website_rss:"  # + site.key  (PER-SITE, rss_guids)
# Single, bounded key overwritten daily with the UTC date of the last successful
# YouTube sweep.
MARKER_YOUTUBE_SWEEP: str = "youtube_sweep"

# --- State filenames ---

STATE_FILE_SEEN_FILINGS: str = "seen_filings.json"
STATE_FILE_SEEN_APPEARANCES: str = "seen_appearances.json"
STATE_FILE_LAST_RUN: str = "last_run.json"
# Pending silent-capture items awaiting the weekly digest. Drained by the
# heartbeat after a successful send.
STATE_FILE_DIGEST_QUEUE: str = "digest_queue.json"

# --- Weekly digest of silent-capture items ---
#
# Events routed to no channels (alert_routing: []) still get queued here so the
# weekly heartbeat can show what was captured. This is the recovery path for a
# first-party appearance on a venue that is not allowlisted: it never alerts
# live, but it is visible within a week.
#
# Hard cap on the on-disk queue. google_news alone can add ~150 ids on a
# catch-up run; the cap keeps the state file bounded if a heartbeat is ever
# missed. When full, the OLDEST entries are dropped -- the newest week is the
# one worth reading.
DIGEST_QUEUE_MAX_ENTRIES: int = 2000
# Per (subject, source) group shown in the email before collapsing to "+N more".
DIGEST_MAX_PER_GROUP: int = 25
# Title truncation inside the digest, so one long headline cannot dominate.
DIGEST_TITLE_MAX_CHARS: int = 110

# --- repository_dispatch bridge (Prompt 6) ---

# GitHub REST API dispatches endpoint. {owner_repo} == "owner/name".
GITHUB_DISPATCHES_URL: str = "https://api.github.com/repos/{owner_repo}/dispatches"
# Env var NAME for the PAT (resolved from os.environ AT CALL TIME, never at import).
ENV_DISPATCH_GITHUB_PAT: str = "DISPATCH_GITHUB_PAT"
# GitHub API version + accept headers (fixed, identify this app -- not tunable).
GITHUB_API_VERSION: str = "2022-11-28"
GITHUB_API_ACCEPT: str = "application/vnd.github+json"
DISPATCH_HTTP_TIMEOUT_SECONDS: float = 20  # POST timeout for the bridge
# Default repository_dispatch event_type if config omits it (config wins).
DEFAULT_DISPATCH_EVENT_TYPE: str = "fomo_monitor_event"
# Max length of a configured event_type (GitHub caps at 100; reject longer at config time).
DISPATCH_EVENT_TYPE_MAX_CHARS: int = 100
# Bridge client_payload schema version (nested envelope tag).
DISPATCH_PAYLOAD_SCHEMA_VERSION: str = "1"
# One transient-retry for the bridge POST (GitHub 502s happen).
DISPATCH_RETRY_ATTEMPTS: int = 1  # retries AFTER the first try
DISPATCH_RETRY_BACKOFF_SECONDS: float = 2.0  # slept via the injected sleep
# Cap on the bridge-error message length carried in the payload (never the PAT).
DISPATCH_ERROR_MAX_CHARS: int = 500

# --- Weekly heartbeat ---
#
# THRESHOLDS ARE CALIBRATED ON OBSERVED CADENCE, NOT ON THE CRON SPEC.
# monitor.yml declares `*/15` (96 runs/day), but GitHub heavily throttles
# high-frequency schedules: measured over 2026-08-19..2026-09-03 this repo
# actually received 2-7 runs/day from 2026-08-27 onward (~6 per weekday), an
# effective gap of roughly 4 hours. A threshold derived from 96/day would fire
# a false alarm on every single heartbeat and train the operator to ignore it.
HEARTBEAT_WINDOW_DAYS: int = 7
# Measured effective gap between delivered scheduled runs (~4h), NOT 15 minutes.
HEARTBEAT_OBSERVED_RUN_GAP_MINUTES: int = 240
# ~6/weekday over 7 days is ~40 runs; alarm only well below that, so ordinary
# throttling variance stays quiet.
HEARTBEAT_MIN_RUNS_PER_WINDOW: int = 20
# A monitor is stale when its last_run is older than this multiple of the LARGER
# of its configured interval and the observed run gap (a 15-minute interval
# cannot beat a 4-hour delivery cadence).
HEARTBEAT_STALE_INTERVAL_MULTIPLIER: int = 3
# Subject prefix for the heartbeat email (distinct from the [ALERT] prefixes so
# it can be filtered into its own inbox folder).
HEARTBEAT_SUBJECT_PREFIX: str = "[MONITOR HEARTBEAT]"
# The monitoring workflow's filename, used to query its run history.
HEARTBEAT_MONITOR_WORKFLOW_FILE: str = "monitor.yml"
# Timeout for the read-only `git` subprocesses the heartbeat shells out to.
HEARTBEAT_GIT_TIMEOUT_SECONDS: int = 30
# GitHub REST endpoint for one workflow's runs. Optional enrichment: used only
# when GITHUB_TOKEN + GITHUB_REPOSITORY are present (i.e. inside Actions).
GITHUB_WORKFLOW_RUNS_URL: str = (
    "https://api.github.com/repos/{owner_repo}/actions/workflows/{workflow}/runs"
)
GITHUB_RUNS_PER_PAGE: int = 100
GITHUB_RUNS_MAX_PAGES: int = 5  # 500 runs is far beyond a 7-day window
ENV_GITHUB_TOKEN: str = "GITHUB_TOKEN"
ENV_GITHUB_REPOSITORY: str = "GITHUB_REPOSITORY"

# --- Run-summary labels (Prompt 6) -- structured logging keys ---
LOG_RUN_SUMMARY_PREFIX: str = "run-summary"

# --- Monitor name set (derived from the MonitorName enum) ---

# The loader requires ``monitor_intervals`` keys to be EXACTLY this set, and
# ``StateStore.record_run`` validates monitor names against it.
MONITOR_NAMES: frozenset[str] = frozenset(m.value for m in MonitorName)

# --- Form-string normalization (EDGAR spelling -> canonical config spelling) ---
#
# EDGAR's submissions JSON spells the Schedule 13D/G forms as "SCHEDULE 13D" /
# "SCHEDULE 13G", while config.yaml (entities[].filing_types) and the
# FILING_TYPE_* maps below use the short "SC 13D" / "SC 13G" spelling. Without
# this alias table the tracked-form filter in ``monitors/edgar.py`` silently
# drops EVERY 13D/13G filing.
#
# Keys AND values are BASE forms only -- never carry an amendment suffix.
# ``_normalize_form`` splits a trailing ``FORM_AMENDMENT_SUFFIX`` off FIRST,
# aliases the base, then re-appends the suffix. So "SCHEDULE 13G/A" -> "SC 13G/A"
# and an amendment stays a DISTINCT form from its base (never collapsed into it).
FORM_BASE_ALIASES: dict[str, str] = {
    "SCHEDULE 13D": "SC 13D",
    "SCHEDULE 13G": "SC 13G",
}

# Trailing amendment marker on an EDGAR form string (uppercase, post-collapse).
FORM_AMENDMENT_SUFFIX: str = "/A"

# --- Filing-type maps (EXACT EDGAR strings, NO wildcards) ---
#
# Consumers look these up with ``.get(filing_type, <FILING_OTHER default>)``.
# Full normalization of raw EDGAR strings (case/whitespace, other amendment
# forms) is deferred to Prompt 3 (the EDGAR monitor). Prompt 1 ships only the
# exact-string maps plus the FILING_OTHER fallback so routing has a stable,
# wildcard-free contract.

FILING_TYPE_PRIORITY: dict[str, Priority] = {
    "13F-HR": Priority.HIGH,
    "13F-HR/A": Priority.HIGH,
    "SC 13D": Priority.HIGH,
    "SC 13D/A": Priority.HIGH,
    "SC 13G": Priority.HIGH,
    "SC 13G/A": Priority.HIGH,
    "4": Priority.MEDIUM,
    "NPORT-P": Priority.MEDIUM,
    # anything else -> resolved by the consumer via .get(filing_type, Priority.LOW)
}

FILING_TYPE_EVENT: dict[str, EventType] = {
    "13F-HR": EventType.FILING_13F,
    "13F-HR/A": EventType.FILING_13F,
    "SC 13D": EventType.FILING_SC13,
    "SC 13D/A": EventType.FILING_SC13,
    "SC 13G": EventType.FILING_SC13,
    "SC 13G/A": EventType.FILING_SC13,
    "4": EventType.FILING_FORM4,
    "NPORT-P": EventType.FILING_OTHER,
    # anything else -> resolved by the consumer via
    # .get(filing_type, EventType.FILING_OTHER)
}
