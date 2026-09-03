# Implementation notes — alerting outage remediation

Append-only. One section per change, newest at the bottom. Records decisions made
where the request was ambiguous, deviations and why, tradeoffs considered, and
open questions for the operator.

Context: alerting had never delivered a single alert since the system went live
(2026-08-09 seed run). Diagnosis in the 2026-09-03 session; this file logs the
remediation batch.

---

## 2026-09-03 — Commit 1: EDGAR form-string aliasing (SCHEDULE 13D/G -> SC 13D/G)

**Problem.** `monitors/edgar.py::_normalize_form` did whitespace/case hygiene
only. EDGAR's submissions JSON spells the Schedule 13D/G forms `"SCHEDULE 13G"`,
while `config.yaml` (`entities[].filing_types`) and `constants.FILING_TYPE_*` use
`"SC 13G"`. The tracked-form filter (`edgar.py::_process_entity`) therefore
dropped every 13D/13G filing with no log line and no event. Confirmed against
live EDGAR: four real filings were silently missed, including a SCHEDULE 13D
disclosing a $523.9M / 21.1% position.

**Decision SD-A1 — alias table lives in `constants.py`, not in the monitor.**
`FORM_BASE_ALIASES` + `FORM_AMENDMENT_SUFFIX` added to `constants.py` per the
project's no-hardcoded-values rule (one place to change when EDGAR adds another
long-form spelling).

**Decision SD-A2 — suffix split/re-append, NOT a flat 4-entry map.** The
alternative was mapping all four strings directly
(`"SCHEDULE 13G/A" -> "SC 13G/A"` etc.). Rejected: it duplicates the amendment
dimension into every future entry, so adding one new base form means adding two
rows and remembering why. Splitting a trailing `/A` first, aliasing the BASE,
then re-appending keeps the table one-row-per-base-form and makes the
"amendments are distinct forms" invariant structural rather than clerical.

**Decision SD-A3 — `endswith` rather than `partition("/A")`.** `partition` finds
the FIRST occurrence, which would mis-split a hypothetical form containing `/A`
mid-string. `endswith` + slice + `rstrip()` only ever treats a TRAILING `/A` as
the amendment marker.

**Explicitly preserved.** Amendments are never collapsed into their base form
(operator requirement): `"SCHEDULE 13G/A"` -> `"SC 13G/A"`, which is a distinct
key from `"SC 13G"` in both `filing_types` and the `FILING_TYPE_*` maps.
`test_normalize_form_amendment_never_collapses_to_base` guards this directly.

**Scope note.** Normalization is applied to BOTH sides of the comparison
(`_process_entity` already normalizes the configured `filing_types` too), so a
config written in either spelling now resolves to the same canonical key. This
widens nothing: `test_edgar_spelled_13g_respects_entity_filing_types` proves an
entity that does not track 13G still does not receive one.

**Open question for the operator.** `config.yaml` does not track `N-PX` for
either entity, so `0001777813-26-000010` (2026-08-31) and `0000935836-26-000464`
(2026-08-28) are correctly skipped. Confirm that is intended — N-PX carries proxy
voting records and may be of interest.

---

## 2026-09-03 — Commit 2: skip unconfigured channels instead of failing them

**Problem.** `main.py` computed `alert_ok = result.event_error is None and not
result.errors`, which required EVERY routed channel to succeed. A routed channel
with no credentials set (SMS with no Twilio secrets) produced an
`AlertError("missing required environment variable(s): ...")` that landed in
`errors`, so the dedupe commit was blocked on an event whose email had already
been delivered — re-alerting the same event on every subsequent run, forever.

**Decision SD-A4 — a new exception type, not string-matching.**
`AlertNotConfiguredError(AlertError)` added to `errors.py` and raised by
`alerting/env.py::_require_env`. Subclassing `AlertError` keeps every existing
`except AlertError` correct (notably `GmailSender.send`'s re-raise arm). The
alternative — sniffing for `"missing required environment"` in the message — was
rejected as brittle and untypeable.

**Decision SD-A5 — three-way channel outcome, not two.** `DispatchResult` gained
`channels_skipped` + `skipped_reasons`. A routed channel now resolves to exactly
one of SENT / SKIPPED / FAILED. `channels_attempted` deliberately EXCLUDES
skipped channels: they were never tried, so counting them as attempted would
misreport what the run actually did.

**Decision SD-A6 — a sender of `None` is also a skip, with a reason.** Previously
`sender is None` was silently `continue`d. It is now reported as SKIPPED with
`REASON_SENDER_DISABLED`. Same operational meaning as unconfigured credentials
(nothing was going to send), and it removes the last silent path through the
dispatcher.

**Decision SD-A7 (INTERPRETATION — operator should confirm) — "no channel
delivered" is NOT a commit.** The instruction was "only genuine send failures on
configured channels should hold back the commit". Read literally, an event whose
channels are ALL unconfigured has no genuine failures and would therefore commit
— marking it seen with nobody ever told, losing it permanently. That is the exact
failure mode this batch exists to end, so `alert_delivered()` additionally
requires `channels_sent` to be non-empty. Net effect: a fully unconfigured
alerting layer holds everything back for retry (correct); a partially configured
one commits on the channel that works (the requested behaviour).

**Decision SD-A8 — skipped channels are logged ONCE per monitor per channel.**
Per-event logging would emit 115 identical lines for a single `google_news` run.
The orchestrator aggregates counts across the batch and emits one WARNING naming
the channel, the event count and the reason.

**Tradeoff accepted.** `alert_delivered` is a module-level public function in
`main.py` rather than a method, so it is unit-testable against a truth table
without constructing a run.

**Not changed.** Per-channel fail-soft isolation inside `dispatch.py` is intact;
one channel's failure still never suppresses the other, and `dispatch_event` /
`dispatch_events` still never raise.

---

## 2026-09-03 — Commit 3: fail loud, with the reason preserved

**Problem, both halves.** (a) The senders wrapped every transport exception in a
constant-string `AlertError("email send failed")`; the cause survived only on
`__cause__`. (b) `main.py` read `result.errors` for truthiness only and logged a
content-free `"alert failed for <id>"`, then returned 0. Net result: a 20-day
total alerting outage was indistinguishable from a transient blip, on runs
GitHub reported as successful.

**Decision SD-A9 — a dedicated `alerting/failure.py`, not inline f-strings.**
`describe_failure(exc, secrets)` renders `"ClassName: message"`. Put in its own
module because BOTH senders need identical redaction semantics and the ordering
guarantee below is a correctness property worth testing in isolation.

**Decision SD-A10 — redact BEFORE capping.** Capping first could slice a secret
in half and leave the surviving fragment in the message. Guarded directly by
`test_secret_is_redacted_before_truncation`, which places a secret at the
truncation boundary and asserts no prefix of length >= 6 survives.

**Decision SD-A11 — the recipient is treated as a secret.** `ALERT_EMAIL` /
`ALERT_PHONE` are deployment secrets, and SMTP echoes the recipient back in
messages like `550 <addr> user unknown`. They are therefore in the redaction set.
TRADEOFF ACCEPTED: diagnosing a wrong recipient address is now slightly harder —
the operator sees `SMTPRecipientsRefused: 550 <redacted> user unknown`. The
exception CLASS still identifies it as a recipient problem, which was judged
sufficient. Reverse this by dropping `to_addr` from the `secrets` list in
`email_alert.send` if the operator prefers legibility over redaction here.

**Decision SD-A12 — raise at the END of the run, not at the point of failure.**
Raising inside `_process_monitor` would abort the remaining monitors, destroying
the fail-soft isolation the operator explicitly asked to keep. `run()` therefore
accumulates failures into a list and raises `AlertDeliveryError` after every
monitor has had its turn. `test_run_raises_after_all_monitors_have_run` proves
the later monitor still runs, dispatches and commits.

**Decision SD-A13 — `AlertDeliveryError` is NOT an `AlertError` subclass.** It is
a run-level summary, not a per-send fault. Keeping it off the `AlertError` tree
guarantees no `except AlertError` arm in the alerting layer can swallow it.

**Decision SD-A14 — uncaught out of `main()`.** The alternative was catching it
in `main()` and returning an exit code. Left uncaught so the traceback, INCLUDING
the chained `__cause__`, reaches the CI log — that chain is precisely what was
being destroyed. Exit code is 1.

**Decision SD-A15 — exception message is a bounded sample.** A `google_news` run
can fail 115 events; the full set is already in the log as one ERROR line each,
so the exception carries `ALERT_FAILURE_SAMPLE_MAX` entries plus a `(+N more)`
suffix.

**REQUIRED companion change — `.github/workflows/monitor.yml`.** The state-commit
step was `if: success()`. With fail-loud, any alert failure would skip the commit
— discarding the dedupe writes for events that DID deliver, and re-alerting them
next run. Changed to `if: ${{ !cancelled() }}`. A cancelled run is still skipped
(its state can be half-written, per the existing concurrency comment), and a run
that failed before `main.py` touched state is a no-op via the existing diff
guard. This is not scope creep: without it, commit 3 introduces the duplicate
alerting that commit 2 exists to prevent.

**Behaviour change for callers.** `run()` now raises instead of always returning
an int. Five existing tests asserted `rc == 0` on a failed-alert path; they now
assert `pytest.raises(AlertDeliveryError)` AND keep their original commit-state
assertions, so the commit semantics are still pinned.

---

## 2026-09-03 — Commit 4: last_run advances only on an observed run

**Problem, two layers deep.** (a) `main.py` called `record_run` from a `finally`
gated only on "the monitor reached its body", so a check that raised mid-way
still stamped a timestamp claiming a successful poll — and the interval gate then
suppressed the monitor until the interval elapsed again. (b) Worse and less
visible: every monitor isolates its sources in a per-unit
`try/except ... continue`, so a run in which EVERY feed/query/page/entity failed
returned normally with zero events and looked *successful* to the orchestrator.

**Decision SD-A16 — a shared `UnitTally`, not per-monitor ad-hoc flags.** New
`monitors/_outcome.py`. All seven monitors share the identical per-unit loop
shape, so seven bespoke booleans would be seven chances to get the edge cases
wrong. `raise_if_total_failure()` raises `MonitorError` iff units were attempted
and none succeeded.

**Decision SD-A17 — `_outcome.py` is its own module, not part of `_common.py`.**
`_common.py` is documented as the RSS-family FEED helper module and `edgar.py`
does not import it. Putting the tally there would have made the EDGAR monitor
depend on the feed scaffold for no reason.

**Decision SD-A18 — only ATTEMPTED units are tallied.** A unit skipped before any
I/O — an empty feed URL in `podcast_rss`, an entity with no configured YouTube
queries — is neither a success nor a failure. So a monitor with nothing to do
never raises, and `attempted == 0` is an explicit no-op in
`raise_if_total_failure`.

**Decision SD-A19 (JUDGEMENT CALL) — a WAF/bot-challenge page counts as a
FAILED unit.** In `conference_pages`, `is_suspect_content` catches a page whose
HTTP fetch succeeded but whose body is a bot-challenge interstitial. The fetch
worked, but the run learned nothing about the page — the same blind spot, for
`last_run` purposes, as a transport error. Counted as a failure. Consequence: if
every conference page is behind a challenge, the monitor raises and retries next
pass rather than silently marking itself polled. Note this is live today —
`state/seen_appearances.json` currently holds a `website:gavinbaker_net` snapshot
whose text is `"Please wait while your request is being verified..."`.

**Decision SD-A20 — `observed`, not `ran`, gates `record_run`.** The distinction
is deliberate and load-bearing:

  * `should_run` failed / not due   -> not recorded (unchanged).
  * `run_check()` raised            -> NOT recorded (the change). Logged at
    WARNING naming the consequence.
  * `run_check()` returned, then dispatch or commit failed -> STILL recorded.
    The poll genuinely succeeded; delivery failure is a separate concern already
    handled by leaving the event un-committed plus the end-of-run raise.

**Test inverted, deliberately.** `test_record_run_happens_even_when_body_raises`
asserted the OLD contract. It is now
`test_failed_check_does_not_advance_last_run` and asserts the opposite, with a
docstring recording that the inversion is intentional. Three sibling tests pin
the other three quadrants of SD-A20.

**Open question for the operator.** `conference_pages` and `website_diff` run on
a 1440-minute interval. With the cron effectively firing ~6x/weekday (GitHub
throttles the `*/15` schedule), a failed daily check now retries on the next
pass, which could be hours later — better than the previous 24-hour blackout,
but still not prompt. Consider whether those intervals are still right.

---

## 2026-09-03 — Commit 5: --replay-since

**DEVIATION FROM THE BRIEF — read this one.** The instruction was "re-emit alerts
for STATE entries newer than the given date". That is not implementable as
written, for two independent reasons:

1. **No timestamps in state.** `seen_filings.json` is `{entity: [accession]}` and
   `seen_appearances.json` is bare id lists. Nothing records WHEN an id was
   added, so "state entries newer than DATE" has no answer. (The only dating
   available is git history of the state commits, which is an artifact of the CI
   workflow, not of the data.)
2. **No content in state.** An entry is an opaque id — `"CBMiqAF..."`,
   `"0001777813-26-000009"`. There is no title, URL, or body to build an alert
   from.
3. **The events worth replaying are NOT in state.** Commit-after-dispatch means
   every event that failed to alert was never recorded. A literal state-driven
   replay would re-send only the alerts that already succeeded and miss all
   twenty days of the ones that did not — the exact inverse of the goal.

**Decision SD-A21 — replay re-runs each monitor against its LIVE source** through
the real production code path, then date-filters the resulting events. This
covers committed AND never-committed events uniformly and needs no schema change.

**Decision SD-A22 — dedupe is bypassed by an emptied-bucket temp store, not by a
flag.** `_replay_state_dir` copies the real state into a `TemporaryDirectory`
with the dedupe buckets EMPTIED but the keys and seed markers KEPT. Empty buckets
make every current item read as "new" (so already-alerted events replay); kept
keys/markers keep each monitor out of its first-run seeding branch, which returns
zero events by design. This required no change to any monitor — the replay
semantics fall out of the existing contract. Configured entities absent from real
state are also given a present-but-empty list so replay works pre-seed.

**Decision SD-A23 — "safe to run repeatedly" means cannot corrupt, not
suppresses.** Replay never writes real state and never fires the bridge, so any
number of runs is harmless. It does re-send on each run — that is the feature.
`test_replay_is_repeatable` pins both halves.

**Decision SD-A24 — hash-diff monitors are excluded entirely.**
`conference_pages` and `website_diff` page-hash events stamp `published = now`
(a content-hash change has no source date), so a date filter would match
everything unconditionally. They are absent from `REPLAYABLE_MONITORS` and
`--monitor website_diff` is a hard `ValueError` rather than a surprise flood.

**Decision SD-A25 — `--limit` keeps the NEWEST N but sends oldest-first.** A
truncated replay should surface the most recent news, but the resulting emails
should still arrive in chronological order. `ReplayReport.matched` reports the
PRE-limit count so the operator can see what was cut.

**Decision SD-A26 — `--dry-run` added (not in the brief).** A command that sends
real email to a real inbox with no undo needs a preview. `--dry-run` lists what
would be sent. Small and opt-in; the default is still the real send path as
specified. Flagging as scope the operator did not ask for.

**Decision SD-A27 — replay is its own module.** `replay.py`, imported lazily
inside `main()`, so a normal cron pass pays nothing for it and the orchestrator
stays thin (per CLAUDE.md). `_build_clients` was renamed to public
`build_clients` so replay can build the same real clients.

**Boundary semantics.** `--replay-since 2026-08-14` is INCLUSIVE of the 14th
(`published.date() >= since`), which is what an operator naming the filing date
expects. Pinned by `test_select_filters_on_and_after_since`.

**Open question for the operator.** Replay of `podcast_rss` / `google_news` /
`cnbc` is only as complete as the source's current window — an RSS feed that has
rolled over no longer carries August items, so replay will find fewer events than
were originally detected. EDGAR has no such limit (full submissions history).
This is inherent to re-running the source and cannot be fixed without recording
event payloads in state.

---

## 2026-09-03 — Commit 6: weekly heartbeat

**Problem.** Absence of alerts was indistinguishable from absence of a working
alerter for 25 days. The heartbeat makes silence legible.

**Decision SD-A28 — thresholds calibrated on OBSERVED cadence (the operator's
explicit instruction).** `monitor.yml` declares `*/15` = 96 runs/day = 672/week.
Measured 2026-08-19..09-03, GitHub actually delivered 2-7 runs/day from 08-27
onward (~6/weekday, ~40/week), an effective gap of ~4 hours. Therefore:

  * `HEARTBEAT_MIN_RUNS_PER_WINDOW = 20` — below the observed ~40/week, so
    ordinary throttling variance stays quiet; a value derived from the cron spec
    would alarm every single week and train the operator to ignore it.
  * `HEARTBEAT_OBSERVED_RUN_GAP_MINUTES = 240` — the staleness budget for a
    monitor is `max(its interval, 240) * 3`. A 15-minute interval cannot beat a
    4-hour delivery cadence, so edgar's budget is 12h, not 45m.

`test_threshold_is_calibrated_on_observed_cadence_not_cron_spec` asserts the
floor sits below the OBSERVED rate and an order of magnitude below the declared
one, so a future edit that "fixes" it against the cron spec fails the suite.

**Decision SD-A29 — two data sources, with graceful degradation.** Preferred:
the Actions API (`GITHUB_TOKEN` + `GITHUB_REPOSITORY`, both present in-workflow)
for an exact executed/failed split — "errors" in the operator's spec is only
answerable this way, since a failed run now still commits state. Fallback: count
`state/` commits, which cannot see failures. The report NAMES which source it
used rather than quietly presenting a weaker number as the same number.

**Decision SD-A30 — "events found" is reported as ALERTS DELIVERED.** Counted as
dedupe-id additions since the window's baseline commit. Dedupe state is written
only after a successful dispatch, so this is a true delivery count, not a
detection count. Detections that failed to alert are deliberately NOT in it —
they show up as run failures instead. Computed by set difference against
`git show <baseline>:state/...`, so JSON key reordering cannot inflate it.

**Decision SD-A31 — `fetch-depth: 0` is mandatory in the workflow.**
`actions/checkout@v4` defaults to a depth-1 shallow clone, which would make the
heartbeat report one run and zero alerts every week — a false alarm generator.
Called out in a comment at the checkout step.

**Decision SD-A32 — the heartbeat never swallows, but never crashes either.**
Every data-source call (`git`, the Actions API) is defensive and degrades to a
zero/None, because a heartbeat that dies on a git fault reintroduces exactly the
silence it exists to prevent. The SEND, by contrast, is allowed to raise: a
heartbeat that cannot be delivered IS the alarm and must fail the job.

**Decision SD-A33 — verdict in the subject line.** `[MONITOR HEARTBEAT] OK — 38
runs, 0 alerts in 7d`. Readable from a phone notification without opening the
mail, and the distinct prefix lets it be filtered away from `[SEC FILING]` etc.

**Validation against the real repo.** Rendered live during development:
38 runs / **0 alerts delivered** over the last 7 days. That is the true number,
and it is exactly the signal that was missing — this heartbeat would have shown
"0 alerts" every week since launch.

**Open question for the operator.** Zero alerts in a week is NOT currently
treated as a problem (a genuinely quiet week is legitimate), only surfaced in the
subject. If a quiet week is implausible enough to be worth alarming on, add a
`HEARTBEAT_MIN_ALERTS_PER_WINDOW` floor.

---

## 2026-09-03 — Commit 7: correct the documented secret names

**Problem, found during the Phase 1 diagnosis.** `README.md` and
`handoffs/2026-07-22-prompt-1-foundation.md` documented `TWILIO_ACCOUNT_SID` /
`TWILIO_AUTH_TOKEN`. No code has ever read those names — `constants.py` defines
`TWILIO_SID` / `TWILIO_AUTH`, and `.github/workflows/monitor.yml` passes those.
An operator setting GitHub secrets from the README would have created two
secrets nothing reads and left the real ones empty.

Fixed immediately rather than logged, per the project's standing instruction on
low-effort issues, and because the operator is setting these secrets right now.
`docs/specs/monitoring_system_spec.md` and
`handoffs/2026-08-01-prompt-6-orchestrator.md` were already correct and are
unchanged.

The README table now names, for every var, the exact constant or config key that
reads it, so the mapping is checkable rather than remembered. The historical
handoff keeps its original line with an inline correction note rather than being
silently rewritten — handoffs are a record of what was believed at the time.

Also documented in the README: the `--replay-since` / `--dry-run` / `--monitor` /
`--limit` CLI, the heartbeat, and the empty-string-secret behaviour (an unset
Actions secret arrives as `""`, which the alerting layer now reads as "channel
not configured" and skips rather than failing).
