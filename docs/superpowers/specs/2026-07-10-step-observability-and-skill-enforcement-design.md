# Step observability + required/recommended skills & tools (Phase 4)

## Problem

Users need three related things `harn run`/studio don't currently give them:

1. **See what the agent is doing, per step** — today's "Run log" is the whole
   background process's raw stdout tail, not scoped to one step; there's no
   readable per-step activity record beyond the one-line `stage_end` summary.
2. **See (and export) the FULL context a step actually works with** — the
   assembled prompt (`_build_step_prompt`'s output) is built and sent to the
   agent but never exposed anywhere for a human to inspect before or after the
   fact, let alone copy to a file.
3. **Declare skills/tools as `recommended` (soft) vs `required` (hard)**, and
   after a step finishes, see which were actually used (🟢 used, 🟡 unused-but-
   optional, 🔴 required-and-unused) — with a required-and-unused skill/tool
   forcing exactly one automatic re-run before escalating to a human.

Pause/resume (originally requested as item 3) is judged ALREADY sufficiently
supported by the existing engine (ledger-driven resume + task-plan editing +
Stop/Relaunch) — this phase only makes it a clearly-labeled UI convenience
(rename Stop's button/messaging), not new engine work.

## Design

### 1 & 2 — Per-step activity log + full-context preview/export

- **Per-step log**: the studio Board/inspector's existing Run-log-style view
  becomes step-scoped — for each step, show its `stage_end` summary (already
  captured) plus the raw turn output tail (already stored where command-step
  output is — extend the SAME `step_results[step_id]` ledger entry to also
  carry an `output` field for AGENT-turn steps, not just command-type steps,
  capping it the same way, e.g. `result.text[-4000:]`). No new event type
  needed — this is exposing data already computed but only surfaced for
  command steps today.
- **Full-context preview**: new function `loop.preview_step_prompt(env_dir,
  cfg, task, step) -> str` — calls the SAME `_build_step_prompt` used for a
  real turn, with no side effects (no turn is run). Exposed via a studio
  backend route (`GET /api/task_plan/step_prompt?task=...&step=...`) and a
  "View full context" button in the step inspector, opening it in a
  read-only panel with a **"Copy to file"** action that POSTs the text to a
  new `save_context_export(env_dir, task_id, step_id, text) -> Path` writing
  `harn_env/state/context_exports/<task_id>_<step_id>_<timestamp>.txt` and
  returns the path so studio can offer a download link.

### 3 — Pause/Resume (UI-only)

Rename the Board's "Stop" button/label to make the resumability explicit
("⏸ Pause — steps already done stay done; edit the plan, then ▶ Resume"), no
change to `runner.stop()`'s mechanics. No spec detail beyond this — it's a
copy/labeling change plus confirming (with a quick manual test) that the
existing Stop → edit-plan → Relaunch path genuinely skips completed steps
(it already does, via `step_results[...]["status"] == "ok"` in `_step_done`).

### 4 — `recommended:` tier for Skills and Tools

Extended step-declaration syntax (backward compatible — the OLD form without
`recommended:` keeps working exactly as today):

```
Skills (required: standards; recommended: testing, ui)
Tools (required: run_tests; recommended: read_design)
```

- `workflow.py`'s `_REQ_RE`/`_TOOLS_RE` are extended to optionally capture a
  `; recommended: a, b` suffix. Parsed node fields gain `skills_recommended:
  list[str]` and `tools_recommended: list[str]` (both `[]` default),
  alongside the EXISTING `required`/`tools` lists (which keep their current
  meaning — `required` fields are unchanged in name and semantics, only a
  sibling "recommended" list is new). The OLD bare `Tools: a, b` line (no
  `(required: ...)` wrapper) becomes sugar for "all recommended, none
  required" for backward compat — existing WORKFLOW.md files parse identically
  to before.

### 5 — Post-step usage audit + enforcement

- **New telemetry**: `state.State` gains `current_step: str | None` (mirrors
  `current_task`), set by the engine right before a step's turn starts and
  cleared after. Every MCP tool that already calls `_context_read` (skills)
  PLUS every other MCP tool (the "any tool counts" answer) emits a NEW event
  `tool_used(task_id, step_id, tool=<name>)` — implemented as a single
  wrapper: the MCP server's tool-registration decorator (or a thin dispatch
  hook already central to all tool calls) tags every invocation, not each
  tool individually (avoids touching 20+ tool functions one by one).
- **Audit, after a step's turn completes**: compare the step's declared
  `required`/`recommended` skill+tool names against the SET of `tool_used`
  (and existing `context_read` for skills) events emitted during that step's
  window (`current_step` scoping already isolates this per step, no
  timestamp-range guessing needed).
  - Required skill/tool NOT used → this step's outcome, regardless of the
    agent's own result, is downgraded to "needs retry": the SAME step re-runs
    ONE more time with an explicit prompt addition ("You did not use required
    skill/tool X — you MUST this time"). Reuses the wave's existing
    `_checkpoint_stage`-then-rerun shape (analogous to the command-step
    retry-once semantics from Phase 2, NOT a new mechanism).
  - Still not used after that ONE retry → `BLOCKED.md` is written by the
    HARNESS itself (not the agent) with a clear message, and the run stops
    exactly like any other block, for the human to resolve.
  - Recommended-and-unused → no retry, just a 🟡 badge (informational).
- **Studio UI**: the step inspector's existing skill/tool toggle chips gain a
  border-color state after a run: green (used), yellow (recommended, unused),
  red (required, unused — pulses until resolved, matching the existing
  `st-active` blink convention).

## Non-goal: required-skill enforcement inside a parallel wave

`state.State` is a single shared file (`env_dir/state/state.json`), not
thread-local — a single `current_step` field cannot represent "N steps active
at once" during a Phase-3 parallel wave (multiple threads, one process). Item
5's enforcement (retry-once, then BLOCKED) is therefore SEQUENTIAL-STEPS-ONLY
in this phase: a step running inside a parallel wave still gets its
`tool_used`/`context_read` events recorded (tagged by the step's own worktree
context, which the agent's turn already carries via `stage=sid`), so the
usage DATA exists and the studio badges (🟢/🟡/🔴) still render correctly for
wave members after the fact — but a required-and-unused skill/tool inside a
wave does NOT trigger an automatic retry-and-possibly-BLOCKED cycle (that
would mean re-running one member of an already-merged wave in isolation,
which is a separate, harder problem deferred past this phase). This is a
deliberate scope cut, not an oversight — revisit only if real usage shows the
gap matters.

## Testing

- `workflow.py`: parse/compose round-trip for `recommended:` on both Skills
  and Tools lines; old bare `Tools: a, b` still parses (as all-recommended);
  old `Skills (required: a, b)` with no `recommended:` still parses (empty
  recommended list).
- `state.py`: `current_step` field round-trips through save/load like
  `current_task`.
- `loop.py`: `preview_step_prompt` returns byte-identical text to what a real
  turn would receive (mock the adapter, compare the prompt argument);
  produces no events/side effects.
- Tool-usage tracking: a sequential step whose turn calls `read_skill` for a
  required skill records a `tool_used`/`context_read` event scoped to THAT
  step (via `current_step`). For a parallel wave (per the Non-goal above),
  confirm usage events are still tagged correctly per-step (e.g. via the
  `stage=sid` already threaded through `_run_turn` for wave members) even
  though enforcement doesn't retry — the DATA must stay correct even where
  the ACTION (retry) is deliberately skipped.
- Enforcement: required-and-unused triggers exactly one retry with the
  reminder text present in the retry prompt; still-unused after retry writes
  `BLOCKED.md` and the run stops; recommended-and-unused never retries.
- Studio: badge colors render correctly for all three states; "View full
  context"/"Copy to file" round-trip (the exported file's content matches
  what was previewed).
