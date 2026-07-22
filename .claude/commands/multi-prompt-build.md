# /multi-prompt-build -- Multi-Prompt Build with Automated QA

Usage: `/multi-prompt-build <workstream> <spec-path-or-instructions>`

Orchestrates a multi-prompt build where each prompt goes through automated QA review before execution. Use when building something that requires multiple sequential prompts (e.g., a multi-step feature build from a spec).

**Arguments passed to this invocation**: `$ARGUMENTS`

Parse arguments: first word is the **workstream name** (e.g., `main`). Everything after is the spec file path or inline instructions.

---

## Governance: Follow This Skill Exactly

The orchestrator must follow this skill exactly as written. Do not add arbitrary timeouts, skip steps, change the QA process, modify the builder agent model, or make any other procedural changes without explicitly asking the user first. If something isn't working as expected, stop and report the problem -- do not improvise a workaround.

---

## Builder Agent Model

Each builder interaction uses the **Agent tool** with `subagent_type: "general-purpose"`. The agent is stateless per call -- the orchestrator holds all state and provides full context in each prompt.

The builder agent:
- Has full codebase access via native tools (Read, Write, Edit, Bash, Grep, Glob)
- Receives all necessary context inline in its prompt (workstream context, spec excerpts, current file state, prior decisions)
- Writes plans and build output to files in the working directory on disk
- Returns results directly to the orchestrator (no session management, no polling)

### How the orchestrator provides context

Instead of the agent running `/resume`, the orchestrator reads the workstream doc, active specs, and latest handoff at the start of the build and injects relevant excerpts into each agent prompt. Keep injected context focused -- include only what the agent needs for its specific task, not the entire workstream doc.

### Agent interactions per prompt

Each prompt may require multiple agent calls:

1. **Plan generation**: Agent receives the build prompt + context, generates a plan, writes it to `{workdir}/prompt-{N}-plan.md`.
2. **Fix/revision** (if needed): Agent receives the current plan + triage decisions, revises the plan, writes to `{workdir}/prompt-{N}-plan-v{V}.md`.
3. **Execution**: Agent receives the approved plan, executes it, writes results to `{workdir}/prompt-{N}-result.md`.

Each of these is a **separate Agent tool call** -- the agent does not retain state between calls. The orchestrator must include all necessary context in each prompt.

### Agent prompt template

Every agent prompt should follow this structure:

```
## Workstream Context
[Relevant excerpts from workstream doc, active specs, prior decisions]

## Current File State
[Key files the agent will read/modify, with paths and brief descriptions]

## Task
[The specific instruction -- generate plan, revise plan, or execute]

## Rules
[Project conventions, constraints from CLAUDE.md that apply]

## Output
[Where to write results -- e.g., "Write the plan to {workdir}/prompt-{N}-plan.md"]
```

### Agent failure handling

If an Agent tool call fails or returns an error:
1. **Stop immediately.**
2. **Show the user** the error message from the agent result.
3. **Ask for direction** -- e.g., "Builder agent failed with [error]. Retry, skip this prompt, or abort?"

Do not silently retry. Do not assume transient failures will resolve.

---

## Failure Handling

If any step fails (agent call, qa-review validators, file operations), do NOT silently retry in a loop or skip the step. Instead:

1. **Stop immediately** after the failure.
2. **Show the user** the exact error output.
3. **Ask for direction** -- e.g., "Agent failed with [error]. Retry, skip this prompt, or abort?"

This applies to:
- Agent tool calls that return errors or produce no plan file
- Fix/revision agents that fail to produce a revised plan
- Execution agents that fail or exit mid-build
- External QA validators (`qa-review` tool) that fail to reach one or more models (Claude, OpenAI, Gemini)

Never assume a transient failure will resolve itself. The user may need to check API keys, network, rate limits, or model availability before continuing.

---

## Timing Instrumentation

Track wall-clock timestamps at each major step using `date +%s` (via Bash tool). Store all timestamps in a bash-sourced file `{workdir}/timing-raw.sh` as variable assignments:

```bash
# Append a timestamp line:
echo "P1_PLAN_START=$(date +%s)" >> {workdir}/timing-raw.sh
# ... later:
echo "P1_PLAN_END=$(date +%s)" >> {workdir}/timing-raw.sh
```

**Global steps to time:**
- `USER_CONFIRM` -- Time waiting for user to confirm the prompt plan (between showing the plan and receiving confirmation)

**Steps to time per prompt** (wrap each with a START/END timestamp):
- `P{N}_PLAN` -- Plan generation (agent call start to plan file written)
- `P{N}_SELF_REVIEW` -- Self-review
- `P{N}_QA{C}` -- Each external QA cycle (qa-review tool call)
- `P{N}_TRIAGE{C}` -- Each triage
- `P{N}_FIX{V}` -- Each fix/revision cycle (fix prompt sent to revised plan received)
- `P{N}_EXEC` -- Execution (approved plan to build complete)

**Format helper**: To compute and display a duration, use:
```bash
DURATION=$((END - START)); printf '%dm %02ds' $((DURATION / 60)) $((DURATION % 60))
```

At the end of the build, generate `{workdir}/timing.md` and display it inline. See the Done Condition section for the output format.

---

## Setup

1. Parse the workstream name and spec/instructions from the arguments.
2. Read the spec or instructions. If a file path, read it. If inline text, use it directly.
3. Read the workstream doc (`workstreams/{workstream}.md`) and extract: current state summary, active specs, relevant conventions. Store this as `workstream_context` for injection into agent prompts.
4. Read the most recent handoff file for context on where the last session left off.
5. Break the work into a sequence of prompts. Each prompt = one logical build step.
6. Create a working directory: `mkdir -p /tmp/multi-prompt-build-$(date +%s)` and store the path. All plans, reviews, and triage notes go here.
7. Create `spec-deviations.md` in the working directory (empty initially). This is for the user's review.
8. Create `implementation-notes.md` in the working directory with a header: `# Implementation Notes\n\nDecisions, deviations, tradeoffs, and open questions logged during build.\n`. Instruct each execution agent to append to this file with timestamps for any decisions where the spec was ambiguous, any deviations and why, any tradeoffs considered, and any open questions for the operator.
9. Initialize timing: `echo "BUILD_START=$(date +%s)" > {workdir}/timing-raw.sh`
10. Record `USER_CONFIRM_START` timestamp, show the prompt sequence, and get confirmation before starting. After user confirms, record `USER_CONFIRM_END` timestamp.

## The Loop (repeat for each prompt)

### 1. WRITE PROMPT
Write the prompt for this build step. You have full context: the spec, constraints, decisions from prior prompts, what's been built so far. The builder agent does not -- your prompt must be self-contained. Include workstream context, current file state, and all necessary details.

### 2. SEND TO BUILDER AGENT (Plan)
Record `P{N}_PLAN_START`. Use the **Agent tool** (`subagent_type: "general-purpose"`) with a prompt that includes:
- Workstream context (excerpts, not the full doc)
- The build prompt with spec requirements
- Current file state (paths and key details)
- Instruction to write the plan to `{workdir}/prompt-{N}-plan.md`
- Explicit instruction: "Do NOT execute yet -- plan only."

After the agent returns, verify the plan file exists. Record `P{N}_PLAN_END`.

### 3. SELF-REVIEW
Record `P{N}_SELF_REVIEW_START`. Read the plan yourself first. Form your own opinion before seeing external feedback: structural issues, contradictions with earlier decisions, missing pieces, over-engineering. Write your assessment to `{workdir}/prompt-{N}-self-review.md`. This prevents anchoring on reviewer opinions. Record `P{N}_SELF_REVIEW_END`.

### 4. EXTERNAL REVIEW
Record `P{N}_QA{C}_START`. Run via Bash:
```
npx tsx tools/qa-review/cli.ts {workdir}/prompt-{N}-plan.md --out {workdir}/prompt-{N}-qa-cycle-{C}.md
```
This fans the plan out to Claude, OpenAI, and Gemini in parallel. Record `P{N}_QA{C}_END`.

### 5. TRIAGE
Record `P{N}_TRIAGE{C}_START`. Read all 3 external reviews against your self-review.

**Keep**: genuine structural issues, missed requirements, bug risks, things multiple reviewers flagged independently.

**Discard**: things explicitly ruled out in the spec, over-engineering suggestions, stylistic preferences, things you already considered and rejected.

Spec violations flagged by reviewers CAN be accepted if all three reviewers identify the same flaw and you agree given full context. But log every spec deviation to `{workdir}/spec-deviations.md` with: what the spec said, what you're doing instead, why, which reviewers flagged it. This file is for user review.

At the end of each triage, explicitly state: `New structural issues found: 0 | 1 | 2+` -- this is what ASSESS uses to decide whether another cycle is warranted.

Write triage decisions to `{workdir}/prompt-{N}-triage-cycle-{C}.md`. Record `P{N}_TRIAGE{C}_END`.

### 6. ASSESS
After every external QA cycle, assess whether the round produced genuinely new structural issues or is just reshuffling minor preferences and repeating prior feedback. If a round produces nothing new, stop iterating on this prompt.

In your triage output, explicitly record: `New structural issues found: 0 | 1 | 2+`

Categorize the combined findings:

- **MAJOR** (any structural issue that changes how components interact, alters data flow, or affects more than one file's implementation): Record `P{N}_FIX{V}_START`. Write fix prompt including all worthwhile fixes (major and minor). Use the **Agent tool** (`subagent_type: "general-purpose"`) -- include the current plan contents, the triage decisions, and instruction to write the revised plan to `{workdir}/prompt-{N}-plan-v{V}.md`. Record `P{N}_FIX{V}_END`. Go back to step 3 (full QA cycle on the revised plan).

- **MODERATE** (1-2 substantive issues, no multi-file structural changes): Record `P{N}_FIX{V}_START`. Write fix prompt including all worthwhile fixes. Use the **Agent tool** with the current plan + triage decisions. Get revised plan. Record `P{N}_FIX{V}_END`. **If the fix changed the plan's structure or logic flow** (added/removed steps, changed component boundaries, altered data flow between modules), run one more external QA cycle on the revised plan. **If the fix was localized** (adding a missing validation, renaming, adjusting an edge case, clarifying language), self-review only and proceed.

- **CLEAN** (trivial or nothing): Proceed.

### 7. HARD CAP: 3 EXTERNAL QA CYCLES PER PROMPT
If you have completed 3 external QA cycles on the same prompt and the latest feedback still contains structural issues:
- Stop. Do not write a fix prompt.
- Show the user the raw feedback from all 3 reviewers.
- Explain why these issues are still surfacing after 2 prior fix rounds -- is the prompt fundamentally flawed, are the reviewers wrong, or is there a spec ambiguity driving it?
- Wait for user direction before continuing.

### 8. EXECUTE
Record `P{N}_EXEC_START`. Use the **Agent tool** (`subagent_type: "general-purpose"`) with a prompt that includes:
- The approved plan (full contents, not just a file path -- the agent may not have read it)
- All workstream context and project conventions needed for implementation
- Instruction to execute the plan and write a summary to `{workdir}/prompt-{N}-result.md`
- Instruction to write a handoff file to `handoffs/` following the project's handoff format

After the agent returns, verify the result file exists and review it. Record `P{N}_EXEC_END`.

### 9. NEXT PROMPT
Read the result file from the completed prompt. Update your context with a summary (not the full output -- keep context lean). Move to the next prompt. The next agent call starts fresh -- include context from prior prompts' results so it knows what was already built.

## Context Management
Write plans, reviews, triage decisions, and build results to files in the working directory. Keep only summaries in conversation context. Full history lives on disk. This prevents context exhaustion across a 5-6+ prompt build.

## Done Condition
"All N prompts have been built, QA'd, and executed" -- or stopped at a hard cap waiting for user input.

When complete:

1. Record `BUILD_END` timestamp.
2. Source `{workdir}/timing-raw.sh` and compute all durations. Generate `{workdir}/timing.md`.
3. Show a summary of all prompts executed, any spec deviations logged, the path to the working directory, and the timing breakdown.
4. Read `{workdir}/implementation-notes.md` and include its full contents verbatim in the output under a `## Implementation Notes` section. Do not summarize or filter -- include the raw file so the operator can review all decisions in context. If the file is empty (only the header) or doesn't exist, explicitly state "No implementation decisions were made outside the spec."

**Timing output format** (display inline and write to `{workdir}/timing.md`):

```
## Timing Breakdown

**Prompt 1** (total: 28m 14s)
  Plan generation:     3m 42s
  Self-review:         0m 18s
  QA cycle 1:          2m 31s
  Triage 1:            0m 22s
  Fix prompt + rev:    4m 15s
  QA cycle 2:          2m 28s
  Triage 2:            0m 19s
  Fix prompt + rev:    3m 58s
  Execution:          10m 21s

**Prompt 2** (total: 14m 48s)
  Plan generation:     2m 55s
  Self-review:         0m 15s
  QA cycle 1:          2m 22s
  Triage 1:            0m 20s
  Fix prompt + rev:    3m 10s
  Execution:           5m 46s

**Wall clock:    47m 02s**
**User wait:      4m 00s** (confirmation prompt)
**Build time:    43m 02s** (wall clock minus user wait)
```

To generate this, source `timing-raw.sh` and compute each duration:
```bash
source {workdir}/timing-raw.sh
DURATION=$((P1_PLAN_END - P1_PLAN_START)); printf '  Plan generation:     %dm %02ds\n' $((DURATION/60)) $((DURATION%60))
# ... repeat for each recorded step
WALL=$((BUILD_END - BUILD_START))
WAIT=$((USER_CONFIRM_END - USER_CONFIRM_START))
BUILD=$((WALL - WAIT))
printf '**Wall clock:    %dm %02ds**\n' $((WALL/60)) $((WALL%60))
printf '**User wait:     %dm %02ds** (confirmation prompt)\n' $((WAIT/60)) $((WAIT%60))
printf '**Build time:    %dm %02ds** (wall clock minus user wait)\n' $((BUILD/60)) $((BUILD%60))
```

Steps that didn't occur (e.g., no fix cycle if the plan was clean) are simply omitted from the output.
