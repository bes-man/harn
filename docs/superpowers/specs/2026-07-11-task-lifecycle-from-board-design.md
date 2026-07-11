# Task lifecycle from the board (Phase 6)

## Problem

Studio's Board tab today can only assign a workflow, launch/stop a run, and
answer a BLOCKED question. It cannot create a task, change a task's status,
or navigate to watch a task's flow execute. Separately, a task's own workflow
plan is snapshotted (frozen into `harn_env/tasks/<id>.workflow.json`)
**eagerly at task creation**, from whatever flow was selected at that
moment (usually none → the project default) — so selecting a different flow
later, before the task has even started, is silently ignored: the frozen
copy already exists and nothing re-creates it.

Users want to: (1) create a task from the board, (2) change a task's status
from the board, (3) be REQUIRED to pick a flow before a task can start, with
that pick actually taking effect, (4) have moving a task to `in_progress`
immediately launch its run, and (5) jump from a task to its Flow-tab view to
watch execution live.

## Non-goals

- Task inheritance / parent-task context inheritance — a separate, later
  phase (explicitly deferred by the user).
- A full review-action UI (Accept / Request changes with comments) in the
  board's new status dropdown — the dropdown does a raw status change only;
  existing review-log-populating helpers (`accept`, `request_changes`) are
  unchanged and remain reachable through their existing callers (CLI/agent).
  Out of scope for this phase.
- Multi-runner / task-queue scheduling — the existing single-runner-at-a-time
  constraint (`runner.py`) is unchanged; a launch attempt while another run
  is active is refused with a clear message, same as today's manual Launch
  button.

## Design

### 1 — Stop freezing a task's plan before it starts

Today: `tasks.create_task()` eagerly calls `workflows.snapshot_for_task()`
at creation time (harn/tasks.py:546), and `harn/studio.py`'s `task_plan_payload`
/`step_prompt_payload` view functions ALSO call `snapshot_for_task()` as a
fallback when no snapshot exists yet — so merely opening the Flow tab to look
at an unstarted task's plan freezes it too.

- **Remove** the eager `snapshot_for_task()` call from `create_task()`. A
  freshly created task has no per-task plan file yet — only `task.workflow`
  (the selected flow's name, `None`/empty = project default), which already
  existed as a field before this phase.
- **New** `workflows.preview_plan(env_dir, task_id, preset) -> dict`: a
  read-only twin of `snapshot_for_task` — if a snapshot already exists,
  return it; otherwise return what a snapshot WOULD contain right now (the
  selected preset's, or default's, nodes) WITHOUT writing the file. Same
  preset-resolution logic as `snapshot_for_task`, just no `save_task_plan`
  call.
- `task_plan_payload` and `step_prompt_payload` (harn/studio.py) switch their
  `load_task_plan(...) or snapshot_for_task(...)` fallback to
  `load_task_plan(...) or preview_plan(...)` — viewing an unstarted task's
  plan (or previewing one step's prompt) never freezes it.
- **Unchanged, deliberately**: `launch_step` (single-step Run/Rerun),
  `run_step`/`run()` in `harn/loop.py` (whole-task execution) keep calling
  `snapshot_for_task()` — actually RUNNING something is exactly the freeze
  point, matching the existing "task.workflow is frozen once execution
  starts" invariant everywhere else in harn.
- **Net effect**: whatever flow is selected on `task.workflow` at the moment
  something first actually executes (a full run OR a single-step Run) is
  the one that gets frozen — never a stale pick from create-time.

### 2 — Create a task from the board

- "＋ New task" button in the Board tab → a small form: Title (required),
  Description (optional, textarea), Flow (`<select>`, optional at this
  stage — the project default is fine to leave selected here; the MANDATORY
  requirement is enforced later, at the `in_progress` transition, not at
  creation), Priority (optional, default 10).
- `POST /api/tasks/create` → `create_task_payload(env_dir, payload) -> dict`
  → `tasks.create_task(env_dir, title, description=..., workflow=..., priority=...)`.
  Task lands in `todo`.

### 3 — Change a task's status from the board

- The task-detail panel gains a status `<select>` (todo / in_progress /
  review / changes_requested / done) next to the existing workflow picker.
- `POST /api/tasks/status` → `set_task_status_payload(env_dir, payload) ->
  dict`. Guards:
  - Refuses (with a clear error) if a run is currently active for THIS task
    (`runner`'s single-active-run state) — the user must Stop first, exactly
    like editing a plan mid-run is already blocked elsewhere.
  - Transitioning TO `in_progress` requires `task.workflow` to be a
    non-empty, valid flow name — see section 4. Attempting to select
    `in_progress` with no flow chosen is refused with a message telling the
    user to pick a flow first (the dropdown itself doesn't disable — the
    route is the single source of truth for this rule, so it's not
    bypassable via a stale client).
  - Otherwise calls `tasks.set_status(task, new_status)` directly (a raw
    status change — no review-log side effects; see Non-goals).

### 4 — Mandatory flow selection, enforced at the `in_progress` transition

- The existing workflow `<select>` in the task-detail panel currently
  defaults silently to "default workflow" when `task.workflow` is empty.
  This stays visually the same (an empty pick still SHOWS as "default
  workflow" in the list — the default is a legitimate, nameable choice, not
  a null state) — the requirement is that the user must have taken SOME
  explicit action to set `task.workflow` (including explicitly choosing the
  default from the list) before the task can move to `in_progress`. Track
  this with a lightweight marker: `task.workflow_confirmed: bool = False` on
  the `Task` dataclass, set `True` the first time `set_task_workflow` is
  called for that task (from the board's `<select>` `onchange` — this
  already exists as a route, just needs the flag added), regardless of which
  flow was picked (including re-picking "default workflow" explicitly).
- `set_task_status_payload`'s `in_progress` guard (section 3) checks
  `task.workflow_confirmed` (not just "is `task.workflow` set") — this is
  what makes the requirement genuinely mandatory rather than satisfied by
  the pre-existing silent default.

### 5 — Auto-launch on `in_progress`

- When `set_task_status_payload` accepts a transition TO `in_progress`
  (guards passed), it — in the same call, synchronously before returning —
  invokes the SAME launch path the existing manual "Launch" button already
  uses (`runner_mod.launch(env_dir.parent, env_dir, task_id, auto=False)`,
  reusing `launch_task`'s existing body rather than duplicating it). If
  `runner.launch` refuses (another run active), the status change ITSELF is
  rolled back (task stays at its prior status) and the launch's error is
  returned — moving to `in_progress` and starting a run are one atomic
  user-visible action, never a status flip with a silently-failed launch
  behind it.

### 6 — Jump to the Flow tab to observe

- Each task row (or the task-detail panel) gets an "Open flow ▶" button:
  switches to the Flow tab and sets `FLOW_SEL_TASK_ID`/`flowTaskSel()`'s
  selection to this task's id (the Flow tab's existing per-task selector,
  already wired to live per-step status via Phase 4's observability work) —
  pure client-side navigation, no new backend route.

## Testing

- `workflows.preview_plan`: returns a snapshot verbatim if one exists (no
  re-write — mtime unchanged); returns the selected preset's nodes without
  creating `<id>.workflow.json` when none exists yet; switching
  `task.workflow` between two preview calls (no snapshot yet) changes what's
  returned each time (proves no premature freeze).
- `create_task` no longer creates a per-task snapshot file at creation —
  `workflows.load_task_plan(env, task.id) is None` immediately after
  `create_task`.
- `task_plan_payload`/`step_prompt_payload` on a freshly created task return
  the live preview (no side-effect file), and picking a different flow
  changes what they return on the next call.
- `launch_step` and `run()`/`run_step` still freeze a real snapshot on
  first actual execution (existing behavior, regression-guarded).
- Status route: refuses `in_progress` when `workflow_confirmed` is false;
  succeeds once `set_task_workflow` has been called at least once (even for
  the default); refuses any status change while a run is active for that
  task; refuses `in_progress` (rolling back the status) when `runner.launch`
  itself refuses (another run active) — task ends up back at its prior
  status, not stuck at `in_progress` with no run.
- Create-task route: creates a real task in `todo`, visible on the next
  board poll.
- End-to-end: create a task, confirm `in_progress` is refused before a flow
  is confirmed, confirm the workflow `<select>`'s `onchange` sets
  `workflow_confirmed`, confirm `in_progress` now launches a real background
  run (assert `runner`'s active-run state reflects it).
