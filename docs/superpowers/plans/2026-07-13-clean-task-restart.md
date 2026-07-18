# Clean Task Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make repeated Run Flow restore git and discard all prior execution context, while Resume remains non-destructive.

**Architecture:** A shared backend reset function owns rollback and execution-state cleanup. Both full rerun routes call it; the UI only warns when the selected task already has a baseline.

**Tech Stack:** Python, git subprocess helpers, vanilla JavaScript Studio UI, pytest.

## Global Constraints

- Preserve the current posted canvas plan when restarting from Run Flow.
- Never clear saved context if git rollback fails.
- Resume must remain non-destructive.
- Bump the harn patch version.

---

### Task 1: Task-scoped transcript cleanup

**Files:**
- Modify: `harn/transcript.py`
- Test: `tests/test_transcript.py`

**Interfaces:**
- Produces: `clear_task(env_dir: Path, task_id: str) -> int`

- [x] Write a failing test that appends entries for two tasks, clears one task, and asserts the other remains.
- [x] Run `pytest tests/test_transcript.py -q` and confirm RED.
- [x] Implement atomic task-scoped filtering of the JSONL store.
- [x] Run `pytest tests/test_transcript.py -q` and confirm GREEN.

### Task 2: Shared clean-restart backend

**Files:**
- Modify: `harn/studio.py`
- Test: `tests/test_step_launch.py`

**Interfaces:**
- Consumes: `transcript.clear_task(env_dir, task_id)` and `loop.rollback(..., apply=True, reopen=True)`.
- Produces: clean restart behavior for `launch_workflow` and `rerun_workflow`.

- [x] Write failing tests proving baseline files are restored and results, attempts, checkpoints, scratchpad, decisions, blocked state, and transcripts are cleared.
- [x] Add a rollback-failure test proving old state is retained and runner launch is not called.
- [x] Run selected tests and confirm RED.
- [x] Implement one shared reset helper and call it before replacing the current canvas snapshot or relaunching the frozen snapshot.
- [x] Run selected tests and confirm GREEN.

### Task 3: Conditional destructive warning

**Files:**
- Modify: `harn/studio.py`
- Test: `tests/test_studio.py`

**Interfaces:**
- Consumes: selected task `baseline_ref`.
- Produces: a confirm dialog only for repeated Run Flow, with explicit context-loss and git-rollback copy.

- [x] Write failing HTML tests for warning presence, copy, and baseline condition; assert Resume has no cleanup call.
- [x] Run selected Studio tests and confirm RED.
- [x] Add the conditional warning before the launch request.
- [x] Run selected Studio tests and confirm GREEN.

### Task 4: Verification and release

**Files:**
- Modify: `pyproject.toml`
- Modify: `harn/__init__.py`

- [x] Bump the patch version.
- [x] Run the affected regression suite.
- [x] Verify the rendered warning through Playwright; the automation accepted the native confirm and the non-git fixture safely rejected the backend restart.
- [x] Run `python -m harn --version`, Python compilation, and `git diff --check`.
