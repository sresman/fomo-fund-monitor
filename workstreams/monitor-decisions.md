# Monitor Decisions

> Companion to `workstreams/monitor.md`. Load on demand, not at startup.
> Append new decisions with date headers. Do not rewrite existing entries.

---
## 2026-09-03 — Alerting root-cause fix + first-party alert policy

Full reasoning for every item below is in `implementation-notes.md`
(SD-A1…SD-A55), which is the primary record. This file carries the decisions a
future session needs to know *exist* without reading that log end to end.

### Why alerting failed for 25 days

Gmail secrets were unset — recoverable on its own. What made it silent was the
reporting path, three layers deep:

1. `GmailSender` wrapped every transport exception in a constant-string
   `AlertError("email send failed")`; the cause survived only on `__cause__`.
2. `Dispatcher` caught it into `DispatchResult.errors`.
3. `main.py` read `errors` for TRUTHINESS ONLY, logged a content-free
   `alert failed for <id>`, and returned 0.

`grep '\.errors'` over production code returned exactly one hit — that
truthiness test. Every run reported `conclusion: success`.

**Rule that follows:** a fail-soft layer is only safe if something above it
READS what it recorded. Fail-soft plus an unread error map is just silence.

### Detection bug found in the same diagnosis

EDGAR spells Schedule 13D/G as `"SCHEDULE 13G"`; config and `FILING_TYPE_*` use
`"SC 13G"`. `_normalize_form` did case/whitespace hygiene only, so the
tracked-form filter dropped **every 13D/13G filing ever seen** — no log line, no
event. Four real filings were missed, incl. a 13D disclosing $523.9M / 21.1% of
SharonAI. Fixed by aliasing the BASE form and re-appending a `/A` suffix, so
amendments stay distinct from their base.

### Alert policy (operator, this session)

Alert set = **first-party appearances by Baker or Leopold, plus SEC filings for
the two tracked entities.** Everything else is captured silently and shown in a
weekly digest. Rationale: Google News was ~92% of alert volume and none of it
actionable. Silence was chosen over disabling the monitor so the dedupe bucket
keeps accruing and re-enabling is a one-line change with no backlog flood.

How first-party is decided:
- **YouTube** — channel allowlisting (`youtube.known_channels`). Title framing no
  longer promotes to HIGH; a title is written by whoever uploaded it, which is
  how a Chinese-language recap and two ~100s clips reached the inbox.
- **podcast_rss** — name in the TITLE, or in the description within 200 chars of
  a guest-framing stem. Stems are PREFIXES (`join` covers joins/joined/joining).

### Two tests that had pinned defects as expected behaviour

Worth remembering as a class of problem: a green suite was concealing both.

- `test_real_manifest_has_zero_youtube_urls` asserted the YouTube dedupe manifest
  yielded zero ids. It did — every url in it was an mp3 — so **the dedupe source
  deduped nothing for the life of the repo**. Now generated from the corpus; 34
  ids, test inverted to a `>= 30` floor and enforced in CI.
- `test_high_surname_and_framing` asserted HIGH for `"Gavin Baker interview"` on
  channel `"Zzz"` — the exact promotion loophole. Inverted.

### Method note

Every filter tightening was blast-radius-checked against real feed data before
being written. That caught two bugs in my own proposals: a framing stem list
without bare `"join"` (would have suppressed 4 genuine Baker appearances, 3 of
them in the transcript corpus), and a backfill that fetched `site.url` where the
`website_diff` monitor fetches `<site.url>/feed`. Do this before changing a
matcher; the cost of suppressing real signal is much higher than a duplicate.

### Standing constraints discovered

- **Substack blocks GitHub Actions runner IPs.** Verified: identical User-Agent,
  HTTP 200 from a laptop, HTTP 403 from a runner. Not fixable with headers.
- **`situational-awareness.com` is unmonitorable by this stack.** `/feed` returns
  homepage HTML; the page normalises to zero characters (client-rendered shell),
  so page-hash diffing fails too. `leopold_post` has never fired.
- **Manifest drift cannot be detected in CI** without checking out the celeb-pm
  corpus, which the operator ruled out. CI enforces *inertness* only; drift is a
  local `tools/build_master_manifest.py --corpus ../celeb-pm --check`.
- **Public-repo schedules are throttled hard** — ~6 runs/weekday against a
  declared 96. Heartbeat thresholds are calibrated to observed cadence, not the
  cron spec, or they would false-alarm every week.
