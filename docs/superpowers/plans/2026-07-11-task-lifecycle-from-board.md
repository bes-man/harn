# Task Lifecycle From The Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user create a task, change its status, and be genuinely required to pick a flow before the task can start — with moving to `in_progress` immediately launching the run — all from studio's Board tab, plus a one-click jump to watch that task's Flow execute.

**Architecture:** A new boolean field on `Task` (`workflow_confirmed`) tracks whether the user has explicitly picked a flow. A new read-only `workflows.preview_plan()` (a non-writing twin of `snapshot_for_task()`) replaces the two studio VIEW functions' snapshot-creating fallback, so looking at an unstarted task's plan never freezes it prematurely — only the pre-existing "task actually executes" call sites (`launch_step`, `loop.run`/`run_step`) still freeze it. Three new backend routes (create/status/— reusing the existing launch route) plus board-tab JS wiring.

**Tech Stack:** Python stdlib only (no new deps), vanilla JS in `harn/studio.py`'s inline `<script>` block, pytest.

## Global Constraints

- Task inheritance / parent-task context inheritance is explicitly OUT OF SCOPE for this plan (a separate, later phase).
- No new review-action UI (Accept / Request changes with comments) — the new status dropdown does a RAW status change only (`tasks.set_status`), never touching `review_log`. The existing `accept`/`request_changes` helpers and their callers are untouched.
- The existing single-runner-at-a-time constraint (`harn/runner.py`) is unchanged — reuse `runner_mod.launch`'s existing refusal behavior, never bypass or duplicate it.
- Every committed change bumps `__version__` in `harn/__init__.py` AND the version in `pyproject.toml` together (current version 0.17.35 — this plan's tasks bump it sequentially, one bump per task's commit).
- No new build step, no new third-party dependencies — vanilla JS only for the studio side, matching `harn/studio.py`'s existing style exactly.

---

### Task 1: `workflow_confirmed` field on `Task` + stop freezing the plan at creation

**Files:**
- Modify: `harn/tasks.py:175` (Task dataclass field), `harn/tasks.py:268` (`_from_dict`), `harn/tasks.py:296` (`_to_dict`), `harn/tasks.py:509-549` (`create_task` — remove the eager snapshot call)
- Test: `tests/test_tasks.py` (or wherever `create_task`/`Task` round-trip is already tested — grep for `def test_create_task` to find the right file)

**Interfaces:**
- Produces: `Task.workflow_confirmed: bool = False` (new field, defaults False, round-trips through `_from_dict`/`_to_dict`/`to_dict`). `create_task(...)` no longer creates `harn_env/tasks/<id>.workflow.json` at creation time.

Today, `harn/tasks.py`'s `Task` dataclass has (currently line 175):
```python
    workflow: str | None = None
```
And `create_task` (currently lines 509-549) ends with:
```python
    _save(task)
    try:
        from . import workflows as workflows_mod
        workflows_mod.snapshot_for_task(env_dir, task.id, workflow)
    except Exception:
        pass   # a failed snapshot must never fail task creation
    return task
```

- [ ] **Step 1: Write the failing tests**

Add to your chosen test file (match its existing `tmp_path`-based fixture style — read a neighboring test first):

```python
def test_task_has_workflow_confirmed_defaulting_false(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tasks.create_task(env, "Do the thing")
    assert t.workflow_confirmed is False


def test_workflow_confirmed_round_trips(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tasks.create_task(env, "Do the thing")
    t.workflow_confirmed = True
    tasks._save(t)
    reloaded = tasks.find(env, t.id)
    assert reloaded.workflow_confirmed is True


def test_create_task_does_not_snapshot_a_plan(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tasks.create_task(env, "Do the thing")
    from harn import workflows
    assert workflows.load_task_plan(env, t.id) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tasks.py -k workflow_confirmed -v` (adjust path to the real file)
Expected: FAIL — `workflow_confirmed` doesn't exist / a plan snapshot IS created (test 3 fails because `load_task_plan` currently returns non-None).

- [ ] **Step 3: Add the field**

In `harn/tasks.py`, the `Task` dataclass, right after the existing `workflow` field (currently line 175):

```python
    workflow: str | None = None
    # Set True the first time the user explicitly picks a flow for this task
    # (including explicitly re-picking the default) — see workflows_mod's
    # preview_plan for why "task.workflow is set" alone isn't enough to make
    # flow selection genuinely mandatory before a task can start.
    workflow_confirmed: bool = False
```

- [ ] **Step 4: Wire it through `_from_dict`/`_to_dict`**

In `_from_dict` (currently line 268, right after `workflow=d.get("workflow") or None,`):

```python
        workflow=d.get("workflow") or None,
        workflow_confirmed=bool(d.get("workflow_confirmed", False)),
```

In `_to_dict` (currently line 296, right after `"workflow": task.workflow,`):

```python
        "workflow":    task.workflow,
        "workflow_confirmed": task.workflow_confirmed,
```

- [ ] **Step 5: Remove the eager snapshot from `create_task`**

Replace the end of `create_task` (currently):

```python
    _save(task)
    try:
        from . import workflows as workflows_mod
        workflows_mod.snapshot_for_task(env_dir, task.id, workflow)
    except Exception:
        pass   # a failed snapshot must never fail task creation
    return task
```

with:

```python
    _save(task)
    return task
```

(The `try/except` and the `workflows_mod` import are no longer needed here — nothing in `create_task` touches workflows anymore. This is intentional: freezing a task's plan now happens only when something actually executes it, see Task 2.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tasks.py -k "workflow_confirmed or create_task" -v`
Expected: PASS — all 3 new tests, plus any pre-existing `create_task` tests still pass (check none of them asserted a snapshot WAS created at creation time — if one does, that assertion is now stale per this task's intentional behavior change; update it to assert `load_task_plan(...) is None` instead).

- [ ] **Step 7: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 567 (baseline) + 3 = 570 passed. If any OTHER test fails because it relied on `create_task` eagerly snapshotting, fix that test's setup to explicitly call `workflows.snapshot_for_task(...)` itself (that function still exists and works exactly as before — only its automatic call from `create_task` was removed).

- [ ] **Step 8: Bump version and commit**

Edit `harn/__init__.py`: `__version__ = "0.17.35"` → `"0.17.36"`. Edit `pyproject.toml` to match.

```bash
git add harn/tasks.py harn/__init__.py pyproject.toml tests/test_tasks.py
git commit -m "feat(tasks): workflow_confirmed field; stop eagerly snapshotting a plan at task creation (Phase 6); version 0.17.36"
```

---

### Task 2: `workflows.preview_plan()` — non-freezing plan preview

**Files:**
- Modify: `harn/workflows.py` (add `preview_plan` right after `snapshot_for_task`, currently ending at line 320)
- Modify: `harn/studio.py:647-658` (`task_plan_payload`), `harn/studio.py:661-681` (`step_prompt_payload`) — swap their fallback
- Test: `tests/test_task_plan.py` (the file already testing `snapshot_for_task`/`load_task_plan` — grep to confirm)

**Interfaces:**
- Consumes: `harn/workflows.py`'s existing `load_task_plan(env_dir, task_id) -> dict | None`, `load(env_dir, name) -> dict | None`, `_ensure_default(env_dir) -> dict` (all already exist, unchanged).
- Produces: `workflows.preview_plan(env_dir: Path, task_id: str, preset: str | None) -> dict` — same return shape as `snapshot_for_task` (`{"preamble": str, "nodes": list}`), but NEVER writes `harn_env/tasks/<id>.workflow.json`. `task_plan_payload`/`step_prompt_payload` in studio.py now call this instead of `snapshot_for_task` when no snapshot exists yet.

Today, `harn/workflows.py`'s `snapshot_for_task` (currently lines 308-320):

```python
def snapshot_for_task(env_dir: Path, task_id: str, preset: str | None) -> dict:
    """Copy the task's chosen preset (or the default) into its own plan file.
    Idempotent: an existing snapshot is returned untouched — a task's plan is
    never silently reset by a second call."""
    existing = load_task_plan(env_dir, task_id)
    if existing is not None:
        return existing
    wf = (load(env_dir, preset) if preset else None) or _ensure_default(env_dir)
    plan = copy.deepcopy({"preamble": wf.get("preamble", ""),
                          "nodes": wf.get("nodes", [])})
    workflow_mod.ensure_ids(plan)
    save_task_plan(env_dir, task_id, plan)
    return plan
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_task_plan.py` (match its existing fixture style):

```python
def test_preview_plan_does_not_create_a_snapshot_file(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tasks.create_task(env, "Do the thing")
    plan = workflows.preview_plan(env, t.id, None)
    assert plan["nodes"] == workflows._ensure_default(env)["nodes"]
    assert workflows.load_task_plan(env, t.id) is None


def test_preview_plan_reflects_a_later_preset_change_before_first_execution(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tasks.create_task(env, "Do the thing")
    workflows.create(env, name="backend", title="Backend",
                     copy_from=None)  # seeds from the default's nodes
    # First preview (no preset chosen yet) sees the default:
    p1 = workflows.preview_plan(env, t.id, None)
    # Second preview (preset now chosen) sees the NEW preset — proving no
    # premature freeze happened on the first call:
    p2 = workflows.preview_plan(env, t.id, "backend")
    assert workflows.load_task_plan(env, t.id) is None   # still no file
    # (p1 and p2 both come from the default's nodes here since "backend" was
    # seeded from it, so assert the call succeeded and created no file — the
    # no-freeze property is what this test protects, not node content drift.)
    assert isinstance(p2["nodes"], list)


def test_preview_plan_returns_the_real_snapshot_once_one_exists(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tasks.create_task(env, "Do the thing")
    real = workflows.snapshot_for_task(env, t.id, None)   # simulates first execution
    seen = workflows.preview_plan(env, t.id, "some-other-preset-name")
    assert seen == real   # once frozen, preview_plan must NOT diverge from it
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_task_plan.py -k preview_plan -v`
Expected: FAIL — `AttributeError: module 'harn.workflows' has no attribute 'preview_plan'`.

- [ ] **Step 3: Implement `preview_plan`**

In `harn/workflows.py`, right after `snapshot_for_task` (after its closing `return plan` at the current line 320):

```python
def preview_plan(env_dir: Path, task_id: str, preset: str | None) -> dict:
    """Read-only twin of snapshot_for_task: if the task already has a frozen
    snapshot, return it verbatim; otherwise return what a snapshot WOULD
    contain right now (the currently-selected preset's, or default's, nodes)
    WITHOUT creating the snapshot file. Powers studio's plan-viewing routes
    for an unstarted task, so looking at (or previewing a step of) a task's
    plan before it has run never freezes a stale pick — only actually
    EXECUTING something (snapshot_for_task's other callers: launch_step,
    loop.run/run_step) does that."""
    existing = load_task_plan(env_dir, task_id)
    if existing is not None:
        return existing
    wf = (load(env_dir, preset) if preset else None) or _ensure_default(env_dir)
    plan = copy.deepcopy({"preamble": wf.get("preamble", ""),
                          "nodes": wf.get("nodes", [])})
    workflow_mod.ensure_ids(plan)
    return plan
```

- [ ] **Step 4: Swap the fallback in `task_plan_payload` and `step_prompt_payload`**

In `harn/studio.py`, `task_plan_payload` (currently lines 647-658):

```python
def task_plan_payload(env_dir: Path, task_id: str) -> dict:
    """The task's OWN workflow plan (harn_env/tasks/<id>.workflow.json) for the
    Board's "Edit this task's plan" button — the same Flow canvas component
    used for presets, just pointed at a per-task snapshot instead."""
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.snapshot_for_task(env_dir, task_id, task.workflow)
    if plan is None:
        return {"ok": False, "error": "no plan"}
    return {"ok": True, "plan": plan, "task_id": task_id}
```

Change the fallback line:

```python
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.preview_plan(env_dir, task_id, task.workflow)
```

In `step_prompt_payload` (currently lines 661-681), the same fallback pattern appears — change it identically:

```python
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.preview_plan(env_dir, task_id, task.workflow)
```

Do NOT touch `launch_step` (around line 745, same fallback pattern) — that one starts real execution of a step and must keep calling `snapshot_for_task` (freezing on purpose). Do NOT touch `harn/loop.py`'s two `snapshot_for_task` call sites (lines 1169 and 1754) for the same reason.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_task_plan.py -v`
Expected: PASS — all tests in the file, including the 3 new ones.

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 570 (prior) + 3 = 573 passed.

- [ ] **Step 7: Bump version and commit**

Edit `harn/__init__.py`: `0.17.36` → `0.17.37`. Edit `pyproject.toml` to match.

```bash
git add harn/workflows.py harn/studio.py harn/__init__.py pyproject.toml tests/test_task_plan.py
git commit -m "feat(workflows): preview_plan() — non-freezing plan view for unstarted tasks (Phase 6); version 0.17.37"
```

---

### Task 3: Studio backend — create task, change status (with mandatory-flow + auto-launch guards)

**Files:**
- Modify: `harn/studio.py` (new `create_task_payload`, `set_task_status_payload`; extend `set_task_workflow` to set `workflow_confirmed`; two new routes)
- Test: `tests/test_board.py` (the file already covering `board_payload`/`launch_task` — grep to confirm; extend it)

**Interfaces:**
- Consumes: `tasks.create_task(env_dir, title, *, description="", workflow=None, priority=10) -> Task` (existing, unchanged signature — confirm exact kwarg names by reading `harn/tasks.py:509-522` before use), `tasks.set_status(task, status) -> None` (existing), `tasks.LIFECYCLE` (existing list of valid statuses), `runner_mod.launch(project_root, env_dir, task_id, *, auto=False) -> dict` (existing, already used by `launch_task`).
- Produces: `create_task_payload(env_dir: Path, payload: dict) -> dict`, `set_task_status_payload(env_dir: Path, payload: dict) -> dict`. Routes `POST /api/tasks/create`, `POST /api/tasks/status`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_board.py` (match its existing `_env(tmp_path)` helper — read a neighboring test first to confirm the exact fixture shape):

```python
def test_create_task_payload_creates_a_todo_task(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.create_task_payload(env, {"title": "Do the thing"})
    assert result["ok"] is True
    t = tasks.find(env, result["task_id"])
    assert t.status == tasks.TODO
    assert t.title == "Do the thing"


def test_create_task_payload_rejects_empty_title(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.create_task_payload(env, {"title": "  "})
    assert result["ok"] is False


def test_set_task_workflow_marks_confirmed(tmp_path):
    env, project_root = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    assert t.workflow_confirmed is False
    studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})
    reloaded = tasks.find(env, t.id)
    assert reloaded.workflow_confirmed is True


def test_status_payload_refuses_in_progress_before_flow_confirmed(tmp_path):
    env, project_root = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "in_progress"})
    assert result["ok"] is False
    assert "flow" in result["error"].lower()
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == tasks.TODO


def test_status_payload_launches_a_run_once_flow_confirmed(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})

    launched = {}
    def fake_launch(pr, ed, task_id, *, auto=False):
        launched["task_id"] = task_id
        return {"ok": True, "pid": 12345, "task_id": task_id, "auto": auto}
    monkeypatch.setattr(studio.runner_mod, "launch", fake_launch)

    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "in_progress"})
    assert result["ok"] is True
    assert launched["task_id"] == t.id
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == "in_progress"


def test_status_payload_rolls_back_if_launch_refuses(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})

    def fake_launch(pr, ed, task_id, *, auto=False):
        return {"ok": False, "error": "a run is already active for OTHER-1"}
    monkeypatch.setattr(studio.runner_mod, "launch", fake_launch)

    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "in_progress"})
    assert result["ok"] is False
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == tasks.TODO   # rolled back, not stuck at in_progress


def test_status_payload_plain_transition_does_not_launch(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")

    def fail_if_called(*a, **kw):
        raise AssertionError("launch should not be called for a non-in_progress transition")
    monkeypatch.setattr(studio.runner_mod, "launch", fail_if_called)

    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "review"})
    assert result["ok"] is True
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == "review"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_board.py -k "create_task_payload or set_task_status_payload or marks_confirmed" -v`
Expected: FAIL — `AttributeError: module 'harn.studio' has no attribute 'create_task_payload'` (and similar for `set_task_status_payload`).

- [ ] **Step 3: Extend `set_task_workflow` to set `workflow_confirmed`**

In `harn/studio.py`, `set_task_workflow` (currently lines 632-644):

```python
def set_task_workflow(env_dir: Path, payload: dict) -> dict:
    """Assign (or clear) a task's workflow preset. Empty -> project default."""
    task_id = (payload.get("task_id") or "").strip()
    t = tasks_mod.find(env_dir, task_id)
    if t is None:
        return {"ok": False, "error": f"no task {task_id}"}
    name = (payload.get("workflow") or "").strip()
    slug = workflows_mod._slug(name) if name else None
    if slug and not any(m["name"] == slug for m in workflows_mod.list_workflows(env_dir)):
        return {"ok": False, "error": f"unknown workflow '{name}'"}
    t.workflow = slug
    tasks_mod._save(t)
    return {"ok": True, "task_id": t.id, "workflow": t.workflow}
```

Add `workflow_confirmed = True` right before the save:

```python
    t.workflow = slug
    t.workflow_confirmed = True
    tasks_mod._save(t)
    return {"ok": True, "task_id": t.id, "workflow": t.workflow}
```

- [ ] **Step 4: Implement `create_task_payload`**

Add near `set_task_workflow`:

```python
def create_task_payload(env_dir: Path, payload: dict) -> dict:
    title = (payload.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    description = payload.get("description") or ""
    workflow = (payload.get("workflow") or "").strip() or None
    try:
        priority = int(payload.get("priority") or 10)
    except (TypeError, ValueError):
        priority = 10
    task = tasks_mod.create_task(env_dir, title, description=description,
                                 workflow=workflow, priority=priority)
    return {"ok": True, "task_id": task.id}
```

- [ ] **Step 5: Implement `set_task_status_payload`**

Add near `set_task_workflow`:

```python
def set_task_status_payload(env_dir: Path, payload: dict) -> dict:
    task_id = (payload.get("task_id") or "").strip()
    new_status = (payload.get("status") or "").strip()
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    if new_status not in tasks_mod.LIFECYCLE:
        return {"ok": False, "error": f"unknown status: {new_status!r}"}
    active = runner_mod.active(env_dir)
    if active and active.get("task_id") == task_id:
        return {"ok": False, "error": "a run is active for this task — stop it first"}
    if new_status == tasks_mod.IN_PROGRESS:
        if not task.workflow_confirmed:
            return {"ok": False,
                    "error": "pick a flow for this task before starting it"}
        prior_status = task.status
        tasks_mod.set_status(task, new_status)
        result = runner_mod.launch(env_dir.parent, env_dir, task_id, auto=False)
        if not result.get("ok"):
            tasks_mod.set_status(task, prior_status)   # roll back — status and
            return {"ok": False, "error": result.get("error", "launch failed")}
        return {"ok": True, "task_id": task_id, "status": new_status,
               "launched": True}
    tasks_mod.set_status(task, new_status)
    return {"ok": True, "task_id": task_id, "status": new_status}
```

- [ ] **Step 6: Wire the two routes**

In `harn/studio.py`'s `do_POST`, add (near the existing `/api/tasks/workflow`/`/api/tasks/launch` branches):

```python
            elif route == "/api/tasks/create":
                self._json(create_task_payload(env, self._read_json()))
            elif route == "/api/tasks/status":
                self._json(set_task_status_payload(env, self._read_json()))
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_board.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 8: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 573 (prior) + 7 = 580 passed.

- [ ] **Step 9: Bump version and commit**

Edit `harn/__init__.py`: `0.17.37` → `0.17.38`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml tests/test_board.py
git commit -m "feat(studio): create-task + status-change routes, mandatory-flow guard, auto-launch on in_progress (Phase 6); version 0.17.38"
```

---

### Task 4: Studio UI — New-task form, status dropdown, Open-flow button

**Files:**
- Modify: `harn/studio.py` (Board tab: a "＋ New task" button/form, the task-detail panel's status `<select>`, an "Open flow ▶" button)

**Interfaces:**
- Consumes: `/api/tasks/create`, `/api/tasks/status` (Task 3), the existing `/api/tasks/workflow` route (now also sets `workflow_confirmed` server-side, no client change needed there).
- Produces: nothing new for later tasks.

- [ ] **Step 1: Add the "＋ New task" button and form**

In `harn/studio.py`'s `renderBoard()` (currently starting at line 1536), add a button right after the opening `<h2>BOARD</h2>` line:

```javascript
function renderBoard(){
  const v=$('#listView');
  const groups={}; (BOARD.tasks||[]).forEach(t=>(groups[t.status]=groups[t.status]||[]).push(t));
  let html='<h2>BOARD</h2>'+
    '<button class="ghost" onclick="showNewTaskForm()" style="margin-bottom:8px">＋ New task</button>'+
    '<div id="newTaskForm" style="display:none"></div>';
```

Add the form-rendering + submit JS near the other board-tab functions (e.g. right after `renderBoard`):

```javascript
function showNewTaskForm(){
  const wfOpts=(S.workflows||[]).map(w=>`<option value="${esc(w.name)}">${esc(w.title)}</option>`).join('');
  const el=$('#newTaskForm');
  el.style.display='block';
  el.innerHTML=`
    <div class="skillrow" style="flex-direction:column;align-items:stretch;gap:6px">
      <input type="text" id="ntTitle" placeholder="Title"/>
      <textarea id="ntDesc" placeholder="Description (optional)" rows="2"></textarea>
      <select id="ntWorkflow"><option value="">default workflow</option>${wfOpts}</select>
      <input type="number" id="ntPriority" placeholder="Priority (default 10)" value="10"/>
      <div class="row" style="gap:6px">
        <button class="primary" onclick="submitNewTask()">Create</button>
        <button class="ghost" onclick="$('#newTaskForm').style.display='none'">Cancel</button>
      </div>
    </div>`;
}
async function submitNewTask(){
  const title=$('#ntTitle').value.trim();
  if(!title){ alert('Title is required.'); return; }
  const r=await post_('/api/tasks/create',{
    title, description:$('#ntDesc').value, workflow:$('#ntWorkflow').value,
    priority:$('#ntPriority').value});
  if(!r.ok){ alert(r.error||'create failed'); return; }
  $('#newTaskForm').style.display='none';
  await pollBoard();
  selectTask(r.task_id);
}
```

- [ ] **Step 2: Add the status dropdown + Open-flow button to the task-detail panel**

In `harn/studio.py`'s `renderTaskDetail()` (currently starting at line 1574), find the status badge line (currently):

```javascript
      <span class="statusbadge" style="color:${statusColor};border-color:${statusColor}">${esc(t.status)}</span>
```

Replace it with a `<select>` that shows the same color and submits on change:

```javascript
      <select onchange="changeTaskStatus('${esc(t.id)}',this.value)" style="color:${statusColor};border-color:${statusColor};background:transparent">
        ${['todo','in_progress','review','changes_requested','done'].map(s=>
          `<option value="${s}" ${t.status===s?'selected':''}>${esc(s)}</option>`).join('')}
      </select>
```

Add the handler JS near `assignWorkflow`/`launchTask` (currently around line 1648):

```javascript
async function changeTaskStatus(taskId,status){
  const r=await post_('/api/tasks/status',{task_id:taskId,status});
  if(!r.ok){ alert(r.error||'status change failed'); await pollBoard(); return; }
  await pollBoard();
}
```

Add the "Open flow ▶" button next to the existing "✎ Edit this task's plan" button (currently around line 1618-1620):

```javascript
    <div class="row" style="margin-top:8px">
      <button class="ghost" onclick="openTaskPlan('${esc(t.id)}')" title="Open this task's own copy of its plan — edits affect only this task">✎ Edit this task's plan</button>
      <button class="ghost" onclick="FLOW_SEL_TASK_ID='${esc(t.id)}';tab='flow';switchTab('flow')" title="Watch this task's flow execute">Open flow ▶</button>
    </div>
```

(Confirm the real name of the tab-switching function — grep `function switchTab` in `harn/studio.py`; if it takes different arguments than `switchTab('flow')`, match its ACTUAL signature instead of guessing. If no such function exists and tab-switching is done some other way — e.g. directly setting `tab='flow'` and calling a render function — use that real mechanism instead.)

- [ ] **Step 3: Verify JS syntax**

Run:
```bash
python3 -c "
import re
html = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', html, re.DOTALL)
open('/tmp/studio_check.js', 'w').write(m.group(1))
"
node --check /tmp/studio_check.js
```
Expected: no output (syntax OK).

- [ ] **Step 4: Manual live verification**

If a live-preview/browser MCP tool is available: start the studio dev server, click "＋ New task", fill in a title, submit, confirm the task appears on the board in `todo`. Select it, try changing status to `in_progress` WITHOUT picking a flow first — confirm it's refused with a clear message and the status stays `todo`. Pick a flow from the workflow `<select>`, then change status to `in_progress` again — confirm it succeeds and a run banner appears (or the task moves to the `in_progress` group). Click "Open flow ▶" — confirm the Flow tab opens with this task selected. If no live tooling is available, verify via careful code tracing instead and say so explicitly in your report.

- [ ] **Step 5: Run the full suite (sanity check — this task is JS-only)**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 580 passed, unchanged.

- [ ] **Step 6: Bump version and commit**

Edit `harn/__init__.py`: `0.17.38` → `0.17.39`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml
git commit -m "feat(studio): New-task form, status dropdown, Open-flow button (Phase 6); version 0.17.39"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md` (EN + RU, following the established bilingual-mirror convention)

**Interfaces:**
- Consumes: nothing — pure documentation.
- Produces: nothing new.

- [ ] **Step 1: Read the current state of the Board-tab documentation**

Grep `README.md` for any existing description of the Board tab / task workflow assignment, to find the right heading level and neighborhood for a new subsection (or a short addition to an existing one).

- [ ] **Step 2: Write the English addition**

Add (as its own small subsection, or appended to wherever the Board tab is already documented):
- "＋ New task" creates a task directly from studio.
- A task's status can be changed from its detail panel.
- Picking a flow is required before a task can move to `in_progress` — the picker itself always shows a selection ("default workflow" is a real, explicit choice, not a silent fallback); moving to `in_progress` is refused until you've picked one (even re-confirming the default counts).
- Moving a task to `in_progress` immediately starts its run (same single-runner-at-a-time rule as the existing Launch button — refused if another run is active).
- "Open flow ▶" jumps to the Flow tab with this task selected, to watch it execute live.

- [ ] **Step 3: Mirror the section in Russian**

Add the same content under `## harn — на русском`, in the same relative position, matching the file's existing bilingual-mirror convention exactly.

- [ ] **Step 4: Run the full suite (sanity check — docs-only change)**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 580 passed, unchanged.

- [ ] **Step 5: Bump version and commit**

Edit `harn/__init__.py`: `0.17.39` → `0.17.40`. Edit `pyproject.toml` to match.

```bash
git add README.md harn/__init__.py pyproject.toml
git commit -m "docs: document task lifecycle from the board — create/status/mandatory-flow/auto-launch (Phase 6); version 0.17.40"
```
