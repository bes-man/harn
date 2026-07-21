# Studio Answer Auto-Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume a Studio-owned task run automatically when a user answers its blocked question in task comments.

**Architecture:** `studio.answer_payload()` already owns the answer route and delegates persistence to `loop.answer()`. It will capture the authoritative blocked task id before clearing state, then call `runner_mod.launch()` for that task. The existing runner single-run guard remains the concurrency boundary; a refused launch becomes a warning after the answer is saved.

**Tech Stack:** Python 3.10+, pytest, Studio's existing `runner_mod` and `loop_mod` modules.

## Global Constraints

- Preserve `loop.answer()` persistence and skill-promotion behavior.
- Resume only `State.current_task`, never a merely selected UI task.
- Do not start a second run when `runner_mod.launch()` refuses because another run is active.
- Keep the existing empty-answer rejection behavior.

---

### Task 1: Resume the blocked Studio task after an answer

**Files:**

- Modify: `tests/test_board.py:312-324`
- Modify: `harn/studio.py:906-916`

**Interfaces:**

- Consumes: `state.State.current_task`, `loop.answer(env_dir, text, source="studio")`, and `runner_mod.launch(project_root, env_dir, task_id)`.
- Produces: `studio.answer_payload(env_dir, task_id, text) -> dict` with `ok=True`, `task_id`, and `resumed=True` after a successful launch; it returns `warning` and `resumed=False` if launch is refused after saving the answer.

- [ ] **Step 1: Write the failing test**

Add `test_answer_route_restarts_the_blocked_task` in `tests/test_board.py`: create a blocked task, patch `harn.studio.runner_mod.launch` to return `{"ok": True}`, submit an answer with a different UI task id, assert the patched launch receives `(env.parent, env, task.id)`, and assert the payload is `{"ok": True, "task_id": task.id, "resumed": True}`.

- [ ] **Step 2: Run test to verify it fails**

Run `pytest -q tests/test_board.py::test_answer_route_restarts_the_blocked_task`. Expected: failure because `runner_mod.launch()` is not called by `answer_payload()`.

- [ ] **Step 3: Write the minimal implementation**

Before calling `loop_mod.answer()`, load `state_mod.State` from `env_dir / "state"` and retain `current_task`. After recording the answer, call `runner_mod.launch(env_dir.parent, env_dir, blocked_task_id)`. Return the successful resume payload; otherwise return the accepted answer with `resumed=False` and the runner error as `warning`.

- [ ] **Step 4: Add the refused-launch regression case**

Add `test_answer_route_keeps_answer_when_resume_is_refused`: patch `runner_mod.launch` to return `{"ok": False, "error": "another run is already active"}`, submit an answer, and assert `ok=True`, `resumed=False`, the exact warning, and that persisted state contains the answer.

- [ ] **Step 5: Run focused tests**

Run `pytest -q tests/test_board.py -k 'answer_route'`. Expected: the existing clear-block test, automatic-resume test, refused-launch test, and empty-answer test all pass.

- [ ] **Step 6: Verify Studio behavior**

Run `pytest -q tests/test_studio.py tests/test_board.py`. Start `python -m harn ui /Users/maximus/projects/harn --port 8777 --no-open`; use Playwright to submit an answer from a blocked-question comment and verify that the question clears and the Board reports an active run.

- [ ] **Step 7: Commit the implementation**

Run `git add harn/studio.py tests/test_board.py` then `git commit -m "fix(studio): resume task after comment answer"`.
