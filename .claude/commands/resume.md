# /resume -- Session Start Command

Usage: `/resume [workstream]` e.g. `/resume main` or `/resume main`

Run at the start of every Claude Code session before touching any code.

**Arguments passed to this invocation**: `$ARGUMENTS`

## Steps

1. CLAUDE.md is already loaded automatically -- do NOT re-read it.

2. Look at the "Arguments passed to this invocation" line above. If it is non-empty, treat each space-separated token as a workstream name. If it is empty (the user ran bare `/resume`), read all workstream docs.

3. Read the relevant workstream doc(s) from `workstreams/`:
   - `workstreams/main.md`
   Do NOT read the `*-decisions.md` companion files at startup; load them mid-session when you hit a question they'd answer.

<!-- Spec loading: each workstream doc has an "Active specs in use" section.
     If it lists spec paths, read those files into context so the session
     has the relevant specs loaded automatically. Skip if empty or placeholder. -->

4. **Load active specs.** For each workstream doc just read, find the "Active specs in use" section. If it contains spec paths (lines matching `` `path/to/spec.md` ``), read each referenced file into context. If the section contains `_No active specs._` or is empty, skip this step.

5. Check for the most recent handoff file in `handoffs/` matching today's workstream(s). Read it -- it has the exact next step from the last session. If multiple workstreams were specified, read the most recent handoff for each.

6. Run `pwd` to confirm working directory. Run `git status` to see uncommitted work.

7. Check for errors in the relevant app only:
   `python -m mypy . --no-error-summary 2>&1 | head -20`

8. Output a session brief:

---
## Session Brief

**Workstream**: [name(s)]
**Current block**: [from workstream doc]
**Last session left off**: [from handoff file, or "no recent handoff -- going from workstream doc"]
**Uncommitted work**: [from git status, or "none"]
**Errors**: [count + summary, or "none"]

**Today's task**: [single specific thing to work on]
**Blocked by anything**: [yes/no -- if yes, explain]
---

Do not start work until brief is confirmed.
