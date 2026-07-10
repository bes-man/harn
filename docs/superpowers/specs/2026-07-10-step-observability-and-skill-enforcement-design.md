# Step observability + required/recommended skills & tools (Phase 4)

## Problem

Users need four related things `harn run`/studio don't currently give them:

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
4. **Answer a BLOCKED question from inside studio itself**, without switching
   to a terminal/chat session — and confirm the existing Telegram-escalation
   mechanism (posting the question there if nobody answers in time) already
   covers "send it with the agent's own embedded recommendation," since the
   question text the agent writes already contains its recommendation as
   prose.

Pause/resume (originally requested as item 3 of the original 5-point request)
is judged ALREADY sufficiently supported by the existing engine (ledger-driven
resume + task-plan editing + Stop/Relaunch) — this phase only makes it a
clearly-labeled UI convenience (rename Stop's button/messaging), not new
engine work.

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

### 6 — Answer a BLOCKED question from studio (+ confirm Telegram escalation)

Today, answering a blocked run requires leaving studio (`harn answer "..."` in
a terminal, or replying in chat/Telegram). This section closes that gap.

- **What already exists, unchanged by this section**: `ask_user(question,
  skill="")` (harn/mcp_server.py) writes agent-authored free-text prose (the
  agent is instructed to embed "context + why, 2-3 options with trade-offs,
  your recommendation" directly IN the question text — there is no separate
  structured options list anywhere) and sets `State.phase = BLOCKED` +
  `State.question`. `_telegram_wait`/`_await_answer` (harn/loop.py) already
  escalate to Telegram after `Config.chat_grace_minutes` and already send the
  full question text verbatim — so "send the question with its embedded
  recommendation to Telegram if nobody answers in time" is ALREADY BUILT and
  needs no engine change. `harn/cli.py`'s `cmd_answer` already calls
  `loop.answer(env_dir, text)`, which clears the block and resumes.
- **What's missing and this section adds**: studio has zero UI for this
  (confirmed via grep — no route, no display, no submit path). Add:
  - A studio backend route `GET /api/tasks/blocked_question?task=<id>` that
    returns `{"question": str}` or `{"question": null}` by loading
    `state.State` for that task (mirrors how `_await_answer` already detects
    "answered" by checking whether the phase left BLOCKED) — read-only,
    reuses the existing 1.5s `pollProgress()` cadence already running in the
    Board (extend that poll's response payload rather than adding a second
    poll loop).
  - When a pending question exists, the Board renders a banner: the question
    text in a `<pre>`-wrapped read-only block (preserves the agent's embedded
    line breaks/options/recommendation exactly as written — plain text, not
    parsed into discrete buttons, since the question is free prose, not
    structured data) plus a textarea and a "Submit answer" button.
  - A new studio backend route `POST /api/tasks/answer` with body
    `{"task": id, "text": str}` that calls the SAME `loop.answer(env_dir,
    text)` function `cmd_answer` already uses — no new answer-clearing logic,
    just a second caller of the existing one.
  - On submit, the banner clears (immediately, optimistically) and the next
    poll tick confirms the run has resumed.

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
- Answer-from-studio: `GET /api/tasks/blocked_question` returns the question
  text while a task is BLOCKED and `null` once answered; `POST
  /api/tasks/answer` clears the block and the task resumes (assert via the
  same mechanism `test_answer`-style CLI tests already use, just through the
  HTTP route instead of the CLI); a task that is NOT blocked returns `null`
  and POSTing to it is a no-op/error, not a crash.
