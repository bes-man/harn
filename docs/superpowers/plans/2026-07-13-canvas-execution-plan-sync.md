# Canvas Execution Plan Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Run progress synchronized with the editable canvas, launch the current flow from either surface, and display every member of parallel waves with shared live status across sidebar and canvas.

**Architecture:** Idle Run progress renders directly from `S.workflow`. A shared launch endpoint replaces the selected task's snapshot with the posted current plan before starting the runner; active execution then reads that frozen snapshot. Sidebar rendering groups consecutive equal `parallel` ids without collapsing member status or transcripts.

**Tech Stack:** Python stdlib, existing harn workflow/task/runner modules, vanilla JavaScript/CSS, pytest, Playwright MCP.

## Global Constraints

- Idle sidebar preview must reflect unsaved `S.workflow` changes immediately.
- Active execution must remain immutable and use the exact plan launched.
- Canvas and sidebar must address status by identical stable step ids.
- Parallel waves count and render every member separately.
- Both Run buttons must call one launch path.
- Preserve historical transcript/results without mixing them into preview.
- Bump the harn patch version after verification.

---

### Task 1: Atomic launch from current canvas plan

**Files:**
- Modify: `harn/studio.py`
- Modify: `tests/test_step_launch.py`

**Interfaces:**
- Produces: `launch_workflow(env_dir, payload)` accepting `task_id`, `workflow`, `plan`, and `auto`.
- Produces: `POST /api/tasks/launch_workflow`.

- [x] Write a failing test where a task has a stale one-step snapshot and the posted plan contains two parallel members; assert the snapshot is replaced before runner launch.
- [x] Run the test and confirm it fails because the route/helper is absent.
- [x] Implement validation, stable ids, snapshot replacement, workflow assignment, and runner launch. Reject an active runner before mutating the snapshot.
- [x] Run `pytest tests/test_step_launch.py -q` and confirm green.

### Task 2: Live preview and shared sidebar launch controls

**Files:**
- Modify: `harn/studio.py`
- Modify: `tests/test_studio.py`

**Interfaces:**
- Consumes: `/api/tasks/launch_workflow`.
- Produces: `launchCurrentFlow()` used by canvas and sidebar.

- [x] Write failing HTML tests proving idle preview uses `S.workflow.nodes`, active execution uses `RUN_HISTORY_PLAN`, and both buttons call `launchCurrentFlow()`.
- [x] Confirm tests fail.
- [x] Replace `runWholeWorkflow` with the shared launch sequence, save dirty canvas first, post the current plan, reset transcript cursor, and open execution mode immediately.
- [x] Add Run/Stop/Resume/Rerun controls to the Run progress header and inline error display.
- [x] Ensure every canvas mutation that calls `renderFlow()` also refreshes an open idle preview.
- [x] Run selected Studio tests and confirm green.

### Task 3: Parallel-wave execution-plan grouping

**Files:**
- Modify: `harn/studio.py`
- Modify: `tests/test_studio.py`

**Interfaces:**
- Produces: `executionPlanGroups(steps)` returning sequential step groups and multi-member wave groups.

- [x] Write failing tests for two consecutive same-wave nodes, a singleton parallel id, and totals counting member steps.
- [x] Confirm tests fail.
- [x] Add wave container CSS and render grouped member cards using the existing per-step status/transcript renderer.
- [x] Preserve individual retry, usage, output, transcript, and state classes for every member.
- [x] Run selected Studio tests and confirm green.

### Task 4: Shared canvas/sidebar runtime status

**Files:**
- Modify: `harn/studio.py`
- Modify: `tests/test_studio.py`

**Interfaces:**
- Consumes: `PROG.stages[stepId]`.
- Produces: `runtimeStepStatus(stepId, ledgerStatus)` used by sidebar while `applyProgress()` colors canvas nodes.

- [x] Write a failing test asserting two active wave ids map to two running sidebar members and `applyProgress()` remains unconditional.
- [x] Implement runtime status precedence: active/done/complete event state overrides a lagging task ledger only during an active run.
- [x] Run Studio tests and confirm green.

### Task 5: Browser verification and release

**Files:**
- Modify: `pyproject.toml`
- Modify: `harn/__init__.py`

- [x] Create a temporary two-command parallel wave with delayed commands.
- [x] In Playwright, edit the canvas and verify the idle sidebar immediately contains both wave members.
- [x] Launch from the sidebar and verify both member cards and both canvas nodes become active during the same interval, then retain independent results.
- [x] Verify sidebar Stop/Run state and canvas polling do not close controls.
- [x] Bump the patch version and run focused regression tests, `python -m harn --version`, and `git diff --check`.
