# fomo-fund-monitor

A standalone Python monitoring system that watches public sources (SEC EDGAR
filings, YouTube, podcast RSS, Google News, CNBC, conference speaker pages, and
website diffs) for signals about a small set of tracked entities, and routes
noteworthy events to email/SMS alerts.

> **Two READMEs, on purpose.** This root `README.md` is the app landing page
> (what it is, how to configure/run). `docs/README.md` is the spec index. Both
> exist deliberately — neither is a stray duplicate.

## Layout (flat, repo-root)

Code modules live at the repo root and are imported top-level (`from config
import ...`), run from the repo root:

```
fomo-fund-monitor/
├── config.py          # typed config loader (config.yaml -> frozen dataclasses)
├── constants.py       # fixed code-level constants (URLs, timeouts, filing maps)
├── models.py          # shared dataclasses + enums (DetectedEvent, Alert, ...)
├── state_manager.py   # StateStore: state files + interval gating
├── errors.py          # ConfigError, StateError, Alert* errors
├── main.py            # orchestrator entrypoint + CLI
├── replay.py          # --replay-since: re-emit past alerts, never mutates state
├── heartbeat.py       # weekly proof-of-life email
├── monitors/          # monitor modules
├── alerting/          # alerter modules
├── config.yaml        # all user-tunable config (NO SECRETS)
├── state/             # committed-back JSON state (seen_* / last_run)
├── reference/         # reference data (master manifest, etc.)
└── tests/             # pytest suite
```

## Configuration

Edit `config.yaml` at the repo root. It holds all user-tunable values (entities,
CIKs, monitor intervals, query sets, feed slots, alert routing, paths). The
loader rejects unknown keys at every level, so `config.yaml` is the authoritative
schema.

### Secrets (environment variables — never in config.yaml)

`config.yaml` stores only the *names* of env vars for alert recipients
(`alert_recipients.email_env`, `.phone_env`); values are resolved from the
environment at send time.

**These names are exact — they are what the code actually reads.** Set them as
GitHub Actions repository secrets with these spellings; `.github/workflows/`
passes them straight through.

| Env var | Read by | Notes |
|---|---|---|
| `ALERT_EMAIL` | `alert_recipients.email_env` → `alerting/dispatch.py` | recipient |
| `ALERT_PHONE` | `alert_recipients.phone_env` → `alerting/dispatch.py` | recipient |
| `GMAIL_USER` | `constants.ENV_GMAIL_USER` | Gmail address to send from |
| `GMAIL_APP_PASSWORD` | `constants.ENV_GMAIL_APP_PASSWORD` | Gmail → Security → App Passwords |
| `TWILIO_SID` | `constants.ENV_TWILIO_SID` | **not** `TWILIO_ACCOUNT_SID` |
| `TWILIO_AUTH` | `constants.ENV_TWILIO_AUTH` | **not** `TWILIO_AUTH_TOKEN` |
| `TWILIO_FROM` | `constants.ENV_TWILIO_FROM` | Twilio phone number |
| `YOUTUBE_API_KEY` | `constants.ENV_YOUTUBE_API_KEY` | YouTube Data API |
| `DISPATCH_GITHUB_PAT` | `constants.ENV_DISPATCH_GITHUB_PAT` | optional; only if `dispatch_bridge.enabled` |

An unset secret is injected by Actions as an EMPTY STRING, which the alerting
layer treats as "channel not configured": that channel is skipped, the others
still deliver, and the skip is logged once per run with the missing var named.

The config loader validates that env-var *names* are non-empty; it does NOT check
that the env vars themselves are set (a send-time concern).

## Running locally

From the repo root (with a `.env` present, or the vars exported):

```bash
python main.py                              # one monitoring pass (what the cron runs)
python main.py --dry-run                    # ... touching no state and sending nothing
```

### Replaying missed alerts

Re-emits alerts for events published on or after a date, through the real send
path. It never writes state and never touches dedupe, so it is safe to run
repeatedly — it re-sends every time, which is the point.

Run it from **GitHub Actions**, not locally: the "Replay Alerts" workflow
(`.github/workflows/replay.yml`) has the working Gmail secrets, so the app
password never needs to exist on a laptop. Actions → Replay Alerts → Run
workflow, set `since`, and untick `dry_run` when you actually want it to send
(it defaults to a preview).

The same CLI exists locally if the env vars are present:

```bash
python main.py --replay-since 2026-08-14 --dry-run     # preview first
python main.py --replay-since 2026-08-14               # edgar only (the default)
python main.py --replay-since 2026-08-14 --monitor youtube --limit 10
```

`--monitor` is repeatable. The high-volume sources (`google_news`, `youtube`,
`podcast_rss`, `cnbc`) replay ONLY when named explicitly — one un-narrowed
`google_news` replay is ~115 emails. `conference_pages` and `website_diff` cannot
be replayed at all: their events carry no source timestamp to filter on.

`--limit N` keeps the N most recent matches but still sends oldest-first.

### Heartbeat

```bash
python heartbeat.py    # emails a 7-day summary; also runs weekly in Actions
```

Reports runs executed (and failed), alerts delivered, and per-monitor time since
last successful observation, so a quiet week is distinguishable from a broken
monitor. Thresholds are calibrated on the OBSERVED run cadence (~6/weekday),
not the `*/15` cron spec, which GitHub throttles heavily.

## Development

CI (`.github/workflows/ci.yml`) runs `mypy` and `pytest` on every push to main
and every PR. The suite also guards the YouTube dedupe manifest: a test asserts
`reference/master_manifest_v2.json` still yields >= 30 YouTube ids, so it cannot
regress to the inert, mp3-only state in which it deduped nothing.

Detecting manifest DRIFT against the celeb-pm corpus needs both checkouts, so it
is a local step rather than CI:

```bash
python tools/build_master_manifest.py --corpus ../celeb-pm --check   # exit 1 if stale
python tools/build_master_manifest.py --corpus ../celeb-pm           # regenerate
```

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

mypy       # config-driven strict type check (do NOT run `mypy .`)
pytest -q  # test suite
```
