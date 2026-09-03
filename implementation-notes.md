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
