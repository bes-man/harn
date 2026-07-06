# Arbitrary workflow stages — Phase 1 (agent + model per step)

## Problem

`harn run` (headless) executes a hardcoded `PIPELINE` of exactly six named
stages (`harn/loop.py:54`, `MODEL_STAGES` in `harn/config.py`):
`plan, execute, test, verify, ui_verify, oracle, reconcile`. Per-stage
model/effort/temperature overrides (`[models.<stage>]` in `harn.toml`) only
work for these six names.

But a user's actual WORKFLOW.md can have an arbitrary number of steps (e.g.
"Business reqs", "System reqs", "Implement", "Tests", "Review", "Release
notes" — 6, 10, 20, whatever the user defines). Each of THOSE steps is
already, architecturally, a separate agent invocation under `harn run`: every
stage's prompt is built fresh from task/scratchpad/skills state
(`_build_prompt`, `_build_verify_prompt`, etc.) — not a continued
conversation. The only thing tying execution to exactly six names is that the
stage list itself is hardcoded, not read from the user's own workflow.

Users need: however many steps they define, each running as its own agent
call with its own agent/model/effort/temperature.

## Non-goals (deferred to Phase 2)

- `Type: command` steps (shell command, no LLM, no tokens) — needs its result
  written into task context for the next step to read.
- `On fail: <step>` — jump to a named step on failure, itself an agent step
  that decides what to do (ask_user, re-run an earlier step, or report and
  stop). Needs a loop-jump guard (analogous to today's `max_iterations`).

Phase 1 assumes every step is an agent turn (matching today's chat-mode
behavior, where "run the tests" is just a tool call inside an agent turn).

## Design

### Data model — WORKFLOW.md per-step fields

New lines per step (same convention as the existing `Skills (required: …)` /
`Tools:` lines), all optional:

```
## 3. Implement
Id: step-3f8a9c
Agent: cursor
Model: composer-1
Effort: high
Skills (required: standards, constraints)
Tools: record_decision, set_scratchpad
```

- `Id:` — a short, stable slug generated once when the step is created.
  Survives renaming (mirrors the existing `Stage:` line's rename-safety).
  Used as the key for git-checkpoints (`gitutil.checkpoint`) and Run/Rerun,
  replacing the closed `MODEL_STAGES` enum as the checkpoint key.
- `Agent:` / `Model:` / `Effort:` / `Temperature:` — this step's overrides.
  Missing = inherit `[harn] agent` / `[harn] model` (Settings tab, already
  shipped). No `Type:` line in Phase 1 (every step is an agent turn); the
  field is reserved for Phase 2 without requiring another migration.

### Config: `harn.toml`'s `[models.<stage>]` tables are removed entirely

`Config.stage_models` / `MODEL_STAGES`-keyed TOML tables go away. The single
source of truth for a step's agent/model/effort/temperature is its WORKFLOW.md
step fields. `harn.toml`'s `[harn] agent` / `[harn] model` remain as the
global fallback (Settings tab). This is a breaking change for any project
already using `[models.<stage>]` — no migration shim, since Phase 1 ships
before this feature has real-world adopters to break.

### Per-task workflow snapshot — the task's own execution plan

WORKFLOW.md / named presets are TEMPLATES. Each task gets its own copy — the
analogue of superpowers' one-plan-per-feature:

- **Snapshot at task creation**: `create_task` copies the chosen preset (or
  the default workflow) into `harn_env/tasks/<task-id>.workflow.json` — the
  same node structure the studio canvas edits. From that moment the task runs
  ONLY its own copy; later edits to the preset affect only future tasks.
- **Visible and editable in the UI**: reuses the SAME Flow canvas component
  used for editing presets, just pointed at a different data source — opened
  from the Board's task detail ("Edit this task's plan") instead of from the
  Flow tab's workflow picker. Before the run: tune steps, agents, models,
  skills for THIS task. During/after: the same canvas renders live per-step
  progress (status dot per node, like today's whole-workflow progress view,
  now per-task instead of per-active-run).
- **Editable mid-flight + restart from any step**: the user can add detail to
  a step's body, attach more skills, then Rerun from that step — the existing
  git-checkpoint mechanism (keyed by step `Id:`) restores the working tree to
  that step's start; steps after it re-run against the edited plan.
- **Per-step ledger on the task**: `task.step_results[step_id] = {status,
  started, ended, tokens, verdict}` — durable progress that survives
  restarts/compaction (the superpowers `progress.md` role), rendered as the
  live pipeline view in the UI.
- **Agent-agnostic contract preserved**: chat agents still read WORKFLOW.md;
  when a task is picked up, its SNAPSHOT (not the preset) is rendered into
  WORKFLOW.md — same `activate` mechanism, new source. The headless engine
  reads the snapshot JSON directly and never touches the global file.

### Execution engine (`harn/loop.py`)

Replace the hardcoded `PIPELINE: list[Stage]` walk with a walk over the
task's workflow snapshot's `enabled` steps, in order. For each step:

1. Build a **generic** prompt from the step's title + body + required skills
   + tools + accumulated task context (scratchpad, prior step results,
   attachments) — replacing the six dedicated `_build_*_prompt` functions
   with one parameterized builder.
2. Resolve the step's adapter (`Agent:` override → else `[harn] agent`) and
   model/effort/temperature (`Model:`/`Effort:`/`Temperature:` → else
   `[harn] model`) — same override-resolution shape as today's
   `_adapter_for_stage`/`_stage_overrides`, now keyed by step `Id:` instead of
   a `MODEL_STAGES` name.
3. Run the turn (`_run_turn`), append a short standing instruction to the
   prompt: "if you learned a durable convention here, call save_to_skill /
   save_service / record_change" — folding today's dedicated `reconcile`
   stage's behavior into every step instead of a separate final stage.
4. Git-checkpoint before the turn (`_checkpoint_stage`), keyed by the step's
   `Id:` — Run/Rerun-per-step keeps working, now for any number of steps.
5. Blocking (`ask_user`), HIL routing (chat/Telegram), and `--auto` mode
   behavior are preserved, generalized to "the current step" rather than
   special-cased per stage name.

`verify`-fail-loops-to-`execute` and oracle's independent-review semantics are
NOT specially preserved as named behaviors in Phase 1 — a workflow that wants
that shape defines it as ordinary steps (e.g. a "Review" step whose prompt
says to check acceptance criteria). Automatic retry-on-fail is Phase 2's
`On fail:`.

**Config gate cleanup.** `Config.planning` / `.verify` / `.oracle` /
`.browser_enabled` currently gate whether a named `PIPELINE` stage runs
(`active_stages()`). With the stage list itself coming from WORKFLOW.md, step
inclusion is just "is this step `enabled`" (already a per-step toggle in the
studio UI) — these four config flags become dead and are removed, along with
`MODEL_STAGES`/`RUN_STAGES`/`active_stages()`/`explain()`'s hardcoded pipeline
view. `harn explain` instead lists the active workflow's steps directly.

### UI (`harn/studio.py`)

- Remove the "Runs as (pipeline stage)" dropdown (`effectiveStage`/
  `nodeStage` keyword-guessing, `RUN_STAGES`/`MODEL_STAGES` gating) entirely.
- **Every** step's inspector shows Agent/Model/Effort/Temperature, unconditionally
  — no more "assign a stage to unlock this" hint, since there's no stage
  concept left to assign.
- Run/Rerun buttons key off the step's `Id:` directly.
- Settings tab (default agent/model) unchanged — still the global fallback.

### Migration for existing projects

A WORKFLOW.md without `Id:`/`Agent:` lines parses as before: every step gets
an auto-generated `Id:` the moment it's first snapshotted into a task (or
saved from studio), with no agent/model override (inherits the global
default) — behaves exactly like today's single-agent default, just no longer
gated to six names.

**In-flight tasks** (created before this ships, with no `workflow_snapshot`
field) keep running exactly as they do today — reading the live global
WORKFLOW.md via `task.workflow` — until they reach a terminal status. The
snapshot mechanism only applies to tasks created after the change; there is
no forced backfill of a snapshot onto an already-running task.

## Testing

- `workflow.py`: parse/compose round-trip for `Id:`/`Agent:`/`Model:`/
  `Effort:`/`Temperature:` lines; rename-survival for `Id:`.
- `loop.py`: the generic per-step prompt builder produces a working prompt;
  step-level agent/model overrides route to the right adapter/kwargs; the
  reconcile-style save-to-skill nudge appears in every step's prompt; git
  checkpoints key correctly off step `Id:` for an arbitrary-length workflow
  (test with e.g. 10+ steps, not just six).
- `studio.py`: every step's inspector renders agent/model controls
  unconditionally; Run/Rerun works keyed by `Id:`.
- Per-task snapshot: `create_task` copies the preset into the task; editing
  the task's plan doesn't touch the preset (and vice versa); `step_results`
  ledger records per-step outcomes; rerun-from-step-N restores the checkpoint
  and re-runs subsequent steps against the EDITED plan.
- Existing tests hardcoding the six-stage assumption (`test_stage_models.py`,
  most of `test_studio_models.py`, the `MODEL_STAGES`-based parts of
  `test_workflow.py`/`test_stage_checkpoints.py`/`test_run_stage.py`) are
  deleted and replaced, not patched in place — the underlying mechanism they
  tested (a closed 6-name enum) no longer exists. Full suite (455 tests as of
  this session) is expected to shrink/shift, not just stay green line-for-line.
