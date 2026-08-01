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
├── errors.py          # ConfigError, StateError
├── main.py            # orchestrator entrypoint (placeholder until Prompt 6)
├── monitors/          # monitor modules (Prompts 3-5)
├── alerting/          # alerter modules (Prompt 2)
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
environment at runtime (Prompts 2/6). Secrets used by later prompts:

- `ALERT_EMAIL`, `ALERT_PHONE` — recipient references
- `GMAIL_USER`, `GMAIL_APP_PASSWORD` — email sending (Prompt 2)
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` — SMS (Prompt 2)
- `YOUTUBE_API_KEY` — YouTube Data API (Prompt 4)

The config loader validates that env-var *names* are non-empty; it does NOT check
that the env vars themselves are set (a runtime concern).

## Running locally

From the repo root:

```bash
python main.py
```

(Currently prints a placeholder; the orchestrator arrives in Prompt 6.)

## Development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

mypy       # config-driven strict type check (do NOT run `mypy .`)
pytest -q  # test suite
```
