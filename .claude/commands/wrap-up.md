# /wrap-up -- Session End Command

Run this at the end of every Claude Code session before closing. Creates a handoff file and updates the workstream doc.

## Steps

1. Compile everything done this session:
   - Files created or modified (from git diff --name-only)
   - Decisions made (architectural, product, or technical)
   - Bugs fixed
   - Known issues discovered but not fixed

2. Check for errors in modified apps. If errors exist, note them -- do not leave the session with silent errors.

3. Check that no hardcoded values were introduced:
   - No hardcoded configuration values outside settings/config modules
   - No hardcoded API endpoints, keys, or secrets
   - No hardcoded timeouts, limits, or thresholds outside config

4. Update the relevant workstream doc in `workstreams/`:
   - Update the status of whatever was worked on today
   - Add new known issues if any were discovered
   - Do NOT rewrite the whole doc -- surgical updates only
   - **Decisions**: Each workstream has a companion `*-decisions.md` file (e.g. `core-decisions.md`). New decisions go there -- append to the relevant section. The main workstream doc keeps only 5-7 key rules in its Settled Decisions summary. Implementation details, API quirks, and bug fix notes belong in the decisions file. Create the decisions file if it doesn't exist yet.

<!-- Spec review: prompt the user to update the "Active specs in use" section
     if any listed specs completed or changed scope during this session.
     Informational only -- the user decides whether to update. -->

5. **Review active specs.** Read the "Active specs in use" section from the workstream doc. For each listed spec, prompt: "Did work on `[spec_path]` complete or change scope this session? If so, update or remove its entry in the workstream file's 'Active specs in use' section." If the section is empty or contains `_No active specs._`, skip this step.

6. Write a handoff file to `handoffs/` named `YYYY-MM-DD-[workstream].md` using this template:

---
## Handoff -- [workstream] -- [date]

**Session duration**: [approximate]
**Workstream**: [name]

### What was built
[Bullet list of files created/modified with one-line description of each]

### Decisions made
[Any architectural, product, or technical decisions made this session. Include the reasoning, not just the decision. Future sessions need to understand WHY.]

### Current state
[Plain English description of where things stand. What works, what doesn't, what's half-built.]

### Known issues
[Bugs, errors, or rough edges discovered but not fixed. Include file + line if known.]

### Next step
[Single specific action to take at the start of the next session. Be exact -- file name, function name, what to do. This is what /resume reads.]

### Parallel work available
[Tasks that have no dependencies on the current workstream and could run simultaneously]

### Context to load
[Any specific files the next session should read before starting -- architecture docs, integration specs, issue links]
---

7. Confirm the handoff file was written and the workstream doc was updated before ending.

Do not end the session without completing this. The next session depends on it.
