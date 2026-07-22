# Monitoring System Spec: Baker & Leopold Appearances + SEC Filings

## Overview

A Python-based monitoring system running on GitHub Actions (cron) that watches for:
1. New public appearances by Gavin Baker (Atreides Management)
2. New public appearances/content by Leopold Aschenbrenner (Situational Awareness)
3. New SEC filings from both entities (13F-HR, 13F-HR/A, SC 13D/G, Form 4, other)

Alerts via email (Gmail SMTP) and SMS (Twilio).

## Architecture

```
celeb-pm-monitor/
├── config.yaml              # All configuration (CIKs, API keys ref, RSS feeds, etc.)
├── state/
│   ├── seen_filings.json     # Filing accession numbers already alerted on
│   ├── seen_appearances.json # YouTube IDs, URLs, episode GUIDs already alerted on
│   └── last_run.json         # Timestamps of last successful check per source
├── monitors/
│   ├── edgar.py              # SEC filing monitor
│   ├── youtube.py            # YouTube API search monitor
│   ├── podcast_rss.py        # RSS feed monitor
│   ├── google_news.py        # Google News / web alert monitor
│   ├── cnbc.py               # CNBC.com video search monitor
│   ├── conference_pages.py   # Conference speaker page diff monitor
│   └── website_diff.py       # gavinbaker.net and other static page monitors
├── alerting/
│   ├── email_alert.py        # Gmail SMTP sender
│   └── sms_alert.py          # Twilio SMS sender
├── main.py                   # Orchestrator — runs all monitors, dedupes, alerts
├── requirements.txt
└── .github/
    └── workflows/
        └── monitor.yml       # GitHub Actions cron workflow
```

## State Management

State files live in the repo and are committed back after each run. This is the simplest approach for GitHub Actions — no external database needed.

`seen_appearances.json`:
```json
{
  "youtube": ["jOgbqt04eUk", "wu-p5xrJ8-E", ...],
  "rss_guids": ["iltb-ep-473-guid", ...],
  "urls": ["https://www.cnbc.com/video/2026/07/20/...", ...],
  "conference_hashes": {"bic_speakers_page": "sha256_of_last_seen_content", ...}
}
```

`seen_filings.json`:
```json
{
  "atreides": ["0001234567-26-000123", ...],
  "situational_awareness": ["0009876543-26-000456", ...]
}
```

After each run, main.py commits updated state files back to the repo via `git add state/ && git commit && git push`. The GitHub Actions workflow needs write permissions to the repo.

---

## Monitor 1: SEC EDGAR (`monitors/edgar.py`)

### Entities to Track

| Entity | CIK | Filing Types |
|--------|-----|-------------|
| Atreides Management (Baker) | Look up via EDGAR company search | 13F-HR, 13F-HR/A, SC 13D, SC 13G, Form 4, NPORT |
| Situational Awareness LP (Leopold) | Look up via EDGAR company search | 13F-HR, 13F-HR/A, SC 13D, SC 13G, Form 4 |

### Implementation

Use EDGAR's EFTS (full-text search) API or the company filings RSS feed:
```
https://efts.sec.gov/LATEST/search-index?q=%22atreides+management%22&dateRange=custom&startdt=YYYY-MM-DD&enddt=YYYY-MM-DD
```

Or the structured filings endpoint:
```
https://data.sec.gov/submissions/CIK{cik_padded}.json
```

This returns all recent filings for the entity. Parse the `recentFilings` array, compare accession numbers against `seen_filings.json`, alert on new ones.

### Polling Frequency

Every 15 minutes. EDGAR rate limit: 10 requests/second with User-Agent header identifying the app. We make ~2-4 requests per run (one per entity, maybe a follow-up fetch), well within limits.

### Alert Content

```
SUBJECT: [SEC FILING] Atreides Management — 13F-HR filed
BODY:
Filing type: 13F-HR
Entity: Atreides Management LLC
Filed: 2026-07-22
Period: 2026-06-30
Accession: 0001234567-26-000123
Link: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=...

This is a quarterly holdings report. The 13F XML with position data
is available at the link above.
```

For 13F specifically: if feasible, also parse the XML and include a quick summary of top position changes vs. prior quarter. This is a stretch goal — the alert itself is the priority.

### Alert Priority

- **13F-HR / 13F-HR/A**: High — email + SMS
- **SC 13D / SC 13G**: High — email + SMS (new large position disclosures)
- **Form 4**: Medium — email only (insider transactions, less urgent)
- **Other**: Low — email only

---

## Monitor 2: YouTube Search (`monitors/youtube.py`)

### Search Queries

Run these searches via YouTube Data API v3 (`search.list`):

**Baker:**
- `"Gavin Baker"`
- `"Gavin Baker" Atreides`
- `"Gavin Baker" semiconductors OR AI`
- `"Atreides Management" interview OR podcast`

**Leopold:**
- `"Leopold Aschenbrenner"`
- `"Leopold Aschenbrenner" Situational Awareness`
- `"Situational Awareness" AI OR AGI`

For each query, request results sorted by date (newest first), limit 5 results. Filter to `type=video`.

### Deduplication

Extract video ID from each result. Check against `seen_appearances.json["youtube"]`. Also check against the master manifest (`master_manifest_v2.json`) YouTube IDs to avoid alerting on already-known appearances.

### Filtering

Exclude results where the person is merely DISCUSSED (reaction videos, commentary). Heuristic: check if the channel matches a known podcast/media channel OR if the video title contains the person's name in a guest/interview framing. Flag uncertain matches as `CONFIDENCE: MEDIUM` in the alert.

### Polling Frequency

Every 30 minutes. YouTube Data API quota: 10,000 units/day. Each search.list call costs 100 units. At ~7 queries × 48 runs/day = 336 calls = 33,600 units — over quota. 

**Solution:** Reduce to every 2 hours (12 runs/day × 7 queries = 84 calls = 8,400 units). Or reduce queries: the two broadest queries (`"Gavin Baker"` and `"Leopold Aschenbrenner"`) cover 90%+ of results. Run just those 2 every 2 hours = 24 calls = 2,400 units. Run the full set once daily as a sweep.

### Alert Content

```
SUBJECT: [NEW VIDEO] Gavin Baker on All-In Podcast — "AI Memory Crunch"
BODY:
Title: E278: AI memory crunch, Micron blowout...
Channel: All-In Podcast
Published: 2026-06-27
Duration: 1:27:22
URL: https://www.youtube.com/watch?v=w8ah_tA0yfg
Confidence: HIGH (name in title + known channel)

Description excerpt: (0:00) Gavin Baker and Travis Kalanick join the show...
```

---

## Monitor 3: Podcast RSS (`monitors/podcast_rss.py`)

### Feeds to Monitor

Based on Baker's recurring shows and the master manifest:

| Show | RSS Feed URL | Check For |
|------|-------------|-----------|
| Invest Like the Best | Megaphone/Colossus RSS | "Baker" or "Atreides" in title/description |
| All-In Podcast | Apple/Megaphone RSS | "Baker" or "Gavin" in title/description |
| This Week in Startups | RSS | "Baker" in title/description |
| Capital Allocators | RSS | "Baker" or "Atreides" |
| a16z Podcast | RSS | "Baker" |
| BG2 Pod | RSS | "Baker" |
| Bankless / Limitless | RSS | "Baker" |
| TBPN | RSS | "Baker" |
| Generating Alpha | RSS | "Baker" |

**For Leopold:**
| Show | RSS Feed URL | Check For |
|------|-------------|-----------|
| Dwarkesh Podcast | RSS | "Leopold" or "Aschenbrenner" |
| Lex Fridman | RSS | "Leopold" or "Aschenbrenner" |
| Any new shows he appears on | Covered by YouTube + Google News monitors |

### Implementation

Use `feedparser` library. For each feed:
1. Parse the RSS
2. Check each entry's `title` and `description` (if present) for name keywords
3. Compare entry GUID or link against `seen_appearances.json["rss_guids"]`
4. Alert on matches

### RSS Feed Discovery

The first time the system runs, it needs the actual RSS URLs. Most podcasts expose these via:
- Apple Podcasts lookup API: `https://itunes.apple.com/search?term=SHOW_NAME&media=podcast`
- The show's website (usually linked in footer or "Subscribe" section)

Store discovered feed URLs in `config.yaml`. This is a one-time setup task.

### Polling Frequency

Every 30 minutes. RSS is free, no API limits, extremely lightweight. Each feed is a single HTTP GET.

### Alert Content

```
SUBJECT: [NEW PODCAST] Gavin Baker on Invest Like the Best — "Watts and Wafers"
BODY:
Show: Invest Like the Best
Episode: Watts and Wafers (EP.473)
Published: 2026-05-20
Audio URL: https://traffic.megaphone.fm/...
Apple Podcasts: https://podcasts.apple.com/...

Description: Patrick O'Shaughnessy sits down with Gavin Baker to explore...
```

RSS alerts will often fire BEFORE YouTube alerts (podcasts typically publish audio 6-24 hours before the YouTube video drops). This is a feature — early warning.

---

## Monitor 4: Google News (`monitors/google_news.py`)

### Implementation

Google News doesn't have an official API. Options:

**Option A: Google Alerts → Email parsing.** Set up Google Alerts for the relevant queries, have them deliver to a dedicated Gmail inbox, and parse incoming alert emails. This is the most reliable long-term approach since Google handles the crawling.

**Option B: SerpAPI or similar.** Use a third-party Google search API ($50/mo for 5,000 searches). More programmatic but costs money.

**Option C: RSS from Google News.** Google News still exposes RSS feeds:
```
https://news.google.com/rss/search?q=%22Gavin+Baker%22+interview+OR+podcast&hl=en-US&gl=US&ceid=US:en
```
Free, no API key, but Google occasionally breaks/throttles these.

**Recommendation:** Start with Option C (free RSS). Fall back to Option A (Google Alerts to email) if Google breaks the RSS.

### Search Queries

```
"Gavin Baker" interview OR podcast OR conference OR panel
"Gavin Baker" Atreides
"Leopold Aschenbrenner" interview OR podcast OR AI
"Situational Awareness" Aschenbrenner
```

### What This Catches

- CNBC.com articles and video embeds
- The Market (themarket.ch) written interviews
- HedgeFundAlpha conference writeups
- Financial press coverage of conference appearances
- Any new venue/platform we haven't seen before
- Bloomberg, Barron's, Institutional Investor profiles

### Polling Frequency

Every 2 hours. Google News RSS is lightweight but don't hammer it.

---

## Monitor 5: CNBC Video Search (`monitors/cnbc.py`)

### Implementation

Periodically search cnbc.com for new Baker/Leopold video appearances. CNBC doesn't have a public API, so this is a scrape:

```
https://www.cnbc.com/search/?query=Gavin+Baker&type=video
```

Parse the search results page for new video URLs. Compare against `seen_appearances.json["urls"]`.

### Polling Frequency

Every 6 hours. Baker's CNBC hits are event-driven (SpaceX IPO, major market moves) — not daily content.

### Why This Is Separate from Google News

CNBC video clips often don't surface in Google News results for days. The direct CNBC search catches them same-day. We have 3 confirmed Baker CNBC appearances (2021, June 2026, July 2026) and the frequency is increasing.

---

## Monitor 6: Conference Speaker Pages (`monitors/conference_pages.py`)

### Pages to Monitor

| Conference | URL to Check | When |
|-----------|-------------|------|
| Boston Investment Conference | `bostoninvestmentconference.com/speakers` | September–November |
| iConnections Global Alts | `iconnections.io` speakers page | November–February |
| Sohn Conference (NY) | `sohnconference.org` speakers page | March–May |
| Sohn Montreal | `sohnmontreal.org` speakers page | March–June |
| Sohn Hearts & Minds (Australia) | `sohnheartsandminds.com.au` | September–December |
| FII Institute / Priority | `fii-institute.org` speakers | Year-round |

### Implementation

Content hashing. For each URL:
1. Fetch the page
2. Hash the content (SHA-256)
3. Compare against `seen_appearances.json["conference_hashes"]`
4. If changed, diff the old and new content and check for "Baker" or "Aschenbrenner"
5. Alert if found

Store the full page text alongside the hash so we can produce a meaningful diff.

### Polling Frequency

Daily. Conference speaker pages update infrequently. During conference season (per the "When" column), increase to every 12 hours.

---

## Monitor 7: Website Diff (`monitors/website_diff.py`)

### Sites to Monitor

| Site | What We're Looking For |
|------|----------------------|
| `gavinbaker.net` | New entries on his podcast/interview list |
| `situational-awareness.com` | New blog posts or essays from Leopold |

### Implementation

Same content-hashing approach as conference pages. For situational-awareness.com, also check RSS if available (Substack sites have RSS at `/feed`).

### Polling Frequency

Daily for gavinbaker.net. Every 6 hours for situational-awareness.com (Leopold publishes infrequently but his posts are high-signal).

---

## Alerting System

### Email (`alerting/email_alert.py`)

Use Gmail SMTP with an app password:
```python
import smtplib
from email.mime.text import MIMEText

def send_email(subject, body, to_addr):
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = GMAIL_USER
    msg['To'] = to_addr
    
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)
```

### SMS (`alerting/sms_alert.py`)

Use Twilio:
```python
from twilio.rest import Client

def send_sms(body, to_phone):
    client = Client(TWILIO_SID, TWILIO_AUTH)
    client.messages.create(
        body=body[:1600],  # SMS limit
        from_=TWILIO_FROM,
        to=to_phone
    )
```

### Alert Routing

| Event Type | Email | SMS |
|-----------|-------|-----|
| 13F-HR / 13F-HR/A filed | Yes | Yes |
| SC 13D / SC 13G filed | Yes | Yes |
| Form 4 filed | Yes | No |
| New YouTube video (HIGH confidence) | Yes | Yes |
| New YouTube video (MEDIUM confidence) | Yes | No |
| New podcast episode (RSS) | Yes | Yes |
| Google News hit | Yes | No |
| CNBC video | Yes | Yes |
| Conference speaker page change | Yes | No |
| Leopold blog post | Yes | Yes |
| Website diff (gavinbaker.net) | Yes | No |

SMS messages are truncated to essentials: type, person, source, URL.

---

## GitHub Actions Workflow

```yaml
name: Celeb-PM Monitor

on:
  schedule:
    # Every 15 minutes for EDGAR
    - cron: '*/15 * * * *'
    # Note: GitHub Actions minimum is 5 min but actual execution 
    # can be delayed. We handle frequency per-monitor in main.py
    # using last_run.json timestamps.
  workflow_dispatch:  # Manual trigger

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          
      - name: Install dependencies
        run: pip install -r requirements.txt
        
      - name: Run monitors
        env:
          YOUTUBE_API_KEY: ${{ secrets.YOUTUBE_API_KEY }}
          GMAIL_USER: ${{ secrets.GMAIL_USER }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          TWILIO_SID: ${{ secrets.TWILIO_SID }}
          TWILIO_AUTH: ${{ secrets.TWILIO_AUTH }}
          TWILIO_FROM: ${{ secrets.TWILIO_FROM }}
          ALERT_PHONE: ${{ secrets.ALERT_PHONE }}
          ALERT_EMAIL: ${{ secrets.ALERT_EMAIL }}
        run: python main.py
        
      - name: Commit state updates
        run: |
          git config user.name "monitor-bot"
          git config user.email "monitor@bot"
          git add state/
          git diff --cached --quiet || git commit -m "Update monitor state $(date -u +%Y-%m-%dT%H:%M:%SZ)"
          git push
```

### Frequency Management

GitHub Actions cron runs every 15 minutes. `main.py` checks `last_run.json` timestamps to determine which monitors actually need to run:

```python
MONITOR_INTERVALS = {
    'edgar': 15,          # minutes
    'youtube': 120,       # 2 hours
    'podcast_rss': 30,
    'google_news': 120,
    'cnbc': 360,          # 6 hours
    'conference_pages': 1440,  # daily
    'website_diff': 1440,
}
```

Each monitor only executes if enough time has elapsed since its last run. This keeps us within API quotas while running the most time-sensitive checks (EDGAR) frequently.

---

## Secrets Required

Store in GitHub repo Settings → Secrets:

| Secret | Source |
|--------|--------|
| `YOUTUBE_API_KEY` | Google Cloud Console → YouTube Data API v3 |
| `GMAIL_USER` | Gmail address for sending alerts |
| `GMAIL_APP_PASSWORD` | Gmail → Security → App Passwords |
| `TWILIO_SID` | Twilio Console |
| `TWILIO_AUTH` | Twilio Console |
| `TWILIO_FROM` | Twilio phone number |
| `ALERT_PHONE` | Steve's phone number |
| `ALERT_EMAIL` | Steve's email for receiving alerts |

---

## Setup Checklist

1. Create a new GitHub repo (`celeb-pm-monitor` or similar)
2. Get YouTube Data API key (free, Google Cloud Console)
3. Create Gmail app password (or use a dedicated monitoring Gmail account)
4. Create Twilio account ($1/mo for phone number, ~$0.01/SMS)
5. Look up CIK numbers for Atreides Management and Situational Awareness LP on EDGAR
6. Discover RSS feed URLs for the ~12 podcast shows listed above
7. Set all secrets in GitHub repo settings
8. Push code and enable Actions

---

## Dependencies

```
# requirements.txt
requests>=2.31
feedparser>=6.0
beautifulsoup4>=4.12
twilio>=8.0
pyyaml>=6.0
lxml>=4.9
```

No heavy dependencies. Everything runs in a few seconds per invocation.

---

## Future Enhancements (Not in v1)

- **Auto-transcription pipeline**: When a new YouTube video is detected, automatically trigger the transcription + thesis extraction pipeline via a webhook or secondary GitHub Action
- **13F diff engine**: When a new 13F is filed, automatically parse the XML, diff against prior quarter, and include top changes in the alert
- **Secondary source scraping for private events**: Around BIC/Sohn dates, automatically search Twitter/X for attendee posts mentioning Baker's pitch
- **Leopold Substack content parsing**: When a new post is detected, summarize key theses in the alert body
- **Dashboard**: Simple HTML page (GitHub Pages) showing the timeline of all detected events and alert history
