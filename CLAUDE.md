# CLAUDE.md -- fomo-fund-monitor

Auto-loaded at session start. Baseline cross-cutting context only.
Subsystem detail lives in `workstreams/*.md` (loaded via `/resume <workstream>`).

---

## Who You're Working With

Solo founder. Builds in focused sessions. Not always in the editor watching diffs -- prefers to delegate and review results. Make reasonable calls independently and flag only genuinely ambiguous things. Fix low-effort issues immediately; don't log them for later.

---

## What This Is

Monitoring system (Python, GitHub Actions cron) that watches for new public appearances by Gavin Baker (Atreides Management) and Leopold Aschenbrenner (Situational Awareness) and new SEC filings from both, alerting via email (Gmail SMTP) and SMS (Twilio).

---

## Project Structure

```
fomo-fund-monitor/
├── docs/                     # Specs, frameworks, reference material
├── workstreams/              # Living docs per workstream + companion *-decisions.md files
├── handoffs/                 # Session handoff files (exact next steps from last session)
└── CLAUDE.md                 # This file
```

## Spec Workflow

- **`docs/README.md`** is the canonical index of all specs, frameworks, and reference material. Human-readable; not loaded ambiently.
- Each workstream file (`workstreams/*.md`) has an **"Active specs in use"** section listing specs currently being implemented.
- `/resume` reads this section and loads referenced spec files into context automatically.
- `/wrap-up` prompts to update or remove entries when work completes.
- When starting work on a spec, add its path to the relevant workstream's "Active specs in use" section.
- When adding a new spec, update `docs/README.md` in the same PR. See `docs/CONTRIBUTING.md` for conventions.
- When implementing any spec, maintain an `implementation-notes.md` in the working directory. Log every decision made where the spec was ambiguous, every deviation from the spec and why, every tradeoff considered, and every open question for the operator. Do not delete or overwrite this file between tasks -- append to it with timestamps.

---

## Tech Stack

- **Language/Framework**: Python + GitHub Actions
- **Test Runner**: pytest

_Expand this section as the stack grows: database, auth, deployment, background jobs, UI framework, etc._

---

## Code Conventions

- **No hardcoded values**: API keys, endpoints, limits, timeouts, configuration go in config/constants files. One place to change.
- **Modular by default**: new features = new module files. Keep orchestrators thin.
- **Type hints everywhere**: use mypy strict mode. No `Any` without justification.
- **Root cause over workarounds**: if a data pipeline produces bad output, fix the data. Don't paper over with hacks.

---

## Debugging & Error Recovery

When something breaks, follow this sequence. Do not skip steps.

1. **STOP** — Do not continue adding features or making other changes.
2. **REPRODUCE** — Make the failure happen reliably. If you cannot reproduce, gather more context before attempting a fix.
3. **LOCALIZE** — Narrow which layer is failing (UI, API, database, build tooling, external service, or the test itself).
4. **REDUCE** — Create the minimal failing case. Strip unrelated code until only the bug remains.
5. **FIX THE ROOT CAUSE** — Fix the underlying issue, not the symptom. Ask "why does this happen?" until you reach the actual cause.
6. **GUARD** — Write a test or add a safeguard that catches this specific failure mode.
7. **VERIFY** — Run the full test suite and build to confirm no regressions.

Do not guess at fixes. Do not rationalize skipping reproduction ("I know what the bug is"). Guessing is right ~70% of the time — the other 30% costs hours.

---

## Scope Discipline

Touch only what the current task requires.

Do NOT:
- "Clean up" code adjacent to your change
- Refactor imports in files you are not modifying
- Remove comments you do not fully understand
- Add features not in the prompt because they "seem useful"
- Modernize syntax in files you are only reading

If you notice something worth improving outside task scope, flag it without fixing it:

```
NOTICED BUT NOT TOUCHING:
- [file] has [issue] (unrelated to this task)
→ Want me to create a separate task for this?
```

---

## Surfacing Ambiguity

When requirements are ambiguous, conflicting, or incomplete, surface it explicitly. Do not silently pick an interpretation.

```
CONFUSION: [describe what is unclear]

Options:
A) [interpretation 1] — [tradeoff]
B) [interpretation 2] — [tradeoff]
C) [ask for clarification because this seems like an intentional decision]

→ Which approach?
```

Check existing code for precedent before asking. If precedent exists, follow it and note the assumption. If no precedent exists, stop and ask.

---

## Honesty Over Agreement

Push back on approaches that have clear problems. Do not soften real issues.

- If a plan has a flaw, say so directly and explain why.
- If an approach will create tech debt or break existing patterns, flag it before implementing.
- "This will work but creates [specific problem]" beats silently implementing something problematic.
- Quantify problems when possible: "This adds ~200ms per request" beats "this could be slow".

---

## Test Conventions

- **Runner**: pytest
- **Mock strategy**: Mock at the client method level, NOT at the HTTP/fetch level. Keeps tests decoupled from transport.
- **Convention**: Each module should have a corresponding test file.
- **Live smoke tests**: Keep in a separate directory, run manually, not in CI.

---

## Cross-Cutting Architectural Rules

_Add rules here as the project evolves. These should apply everywhere and override local convenience._

---

## What Not to Do

_Add prohibitions here as patterns emerge. These are the "never do this" rules learned from experience._
