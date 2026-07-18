# Studio Board Kanban Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Studio's board into a real Kanban board — columns per configured status (including empty ones), native drag-and-drop that changes status on drop, and a task detail modal (replacing the side panel) with editable key fields and a merged Comments/Activity feed.

**Architecture:** Backend first (labels field, Comment model, config setting, two new partial-update routes, a HIL-answer→comment hook), all independently unit-tested against `harn/tasks.py`/`config.py`/`studio.py`. Frontend last, entirely inside `studio.py`'s `_HTML` string (vanilla JS, no new dependency) — column rendering, HTML5 drag-drop, and the modal are each one self-contained JS change verified by a regression test asserting the served markup/JS contains the right hooks (matching the existing pattern in `tests/test_board_statuses.py`'s `test_studio_html_renders_board_order_as_javascript_var`).

**Tech Stack:** Python stdlib, existing `harn` module conventions (dataclasses + JSON-per-line frontmatter, `http.server`-based Studio, vanilla JS in a single `_HTML` template string — no CDN, no bundler, no new pip dependency).

## Global Constraints

- No new third-party dependency (drag-drop uses the native HTML5 Drag & Drop API).
- Every backend change keeps a no-config / no-drag-config project behaving exactly as today (labels default `[]`, comments default `[]`, `launch_on_drag_in_progress` defaults `false`).
- `POST /api/tasks/update` never changes `status` — status changes stay on the existing `/api/tasks/status` route (drag reuses it) to avoid two code paths mutating the same field.
- Follow `docs/superpowers/specs/2026-07-18-studio-board-kanban-design.md` exactly; if an ambiguity surfaces mid-implementation, resolve it the way the spec's "Out of scope" section would (prefer doing less).

---

### Task 1: `Task.labels` field (data model)

**Files:**
- Modify: `harn/tasks.py`
- Test: `tests/test_board_kanban.py` (new file)

**Interfaces:**
- Produces: `Task.labels: list[str]` (default `[]`), present in `to_dict()["labels"]`, round-trips through frontmatter, settable via `create_task(..., labels=[...])`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_board_kanban.py
"""Studio board Kanban redesign (docs/superpowers/specs/2026-07-18-studio-board-kanban-design.md):
labels, comments, partial task updates, drag-triggered launch gating."""
from __future__ import annotations

from harn import config as config_mod
from harn import studio, tasks, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


def test_labels_default_to_empty_list(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    assert t.labels == []
    assert tasks.to_dict(t)["labels"] == []


def test_labels_set_at_creation_and_round_trip(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it", labels=["bug", "urgent"])
    assert t.labels == ["bug", "urgent"]
    reloaded = tasks.find(env, t.id)
    assert reloaded.labels == ["bug", "urgent"]


def test_labels_editable_after_creation(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    t.labels = ["design"]
    tasks._save(t)
    assert tasks.find(env, t.id).labels == ["design"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_board_kanban.py -v`
Expected: FAIL — `create_task() got an unexpected keyword argument 'labels'` (or `AttributeError: 'Task' object has no attribute 'labels'`).

- [ ] **Step 3: Add the field**

In `harn/tasks.py`, add to the `Task` dataclass (near `prds`/`depends_on`, both `list[str] = field(default_factory=list)`):

```python
    labels:      list[str] = field(default_factory=list)
```

Add `"labels"` to `_FRONTMATTER_FIELDS`:

```python
_FRONTMATTER_FIELDS = ("id", "title", "status", "priority", "workflow",
                       "prds", "depends_on", "external", "labels")
```

In `_render_frontmatter`'s `values` dict, add `"labels": task.labels,`.

In `_build_task`, add `labels=list(fm.get("labels") or []),` alongside the existing `depends_on=list(fm.get("depends_on") or []),` line.

In `to_dict()`, add `"labels": task.labels,` alongside `"depends_on": task.depends_on,`.

Find `create_task(...)` in `harn/tasks.py` (signature has `prds`, `skills`, etc. as optional list params) and:
- add a `labels: list[str] | None = None` parameter,
- pass `labels=list(labels or [])` into the `Task(...)` construction it builds.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_board_kanban.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run the full suite (regression check)**

Run: `python3 -m pytest -q`
Expected: all passing, same count as baseline + 3.

- [ ] **Step 6: Commit**

```bash
git add harn/tasks.py tests/test_board_kanban.py
git commit -m "feat(tasks): add labels field to task frontmatter"
```

---

### Task 2: `Comment` dataclass + `Task.comments` + `## Comments` section

**Files:**
- Modify: `harn/tasks.py`
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: the `_bullets`/`_render_bullets` helpers already used by `Decision`/`ReviewEntry`/`ChangeEntry` (each JSON-per-line under a `## <Section>` heading), and `_KNOWN_SECTIONS`/`_SECTION_HEADER_SET`/`_split_body`'s exact-heading-line matching.
- Produces: `Comment(ts, author, text, kind)` with `.to_dict()`, `Task.comments: list[Comment]` (default `[]`), a `## Comments` markdown section rendered between `## Review log` and `## Changelog` (placement irrelevant functionally — sections are matched by heading, not position — but keep insertion order stable so a human reading the raw `.md` sees comments where they'd expect: right after Review log).

- [ ] **Step 1: Write the failing tests**

```python
def test_comment_round_trips_through_comments_section(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    assert t.comments == []
    tasks.add_comment(env, t, "please use postgres", author="maxim", kind="human")
    reloaded = tasks.find(env, t.id)
    assert len(reloaded.comments) == 1
    c = reloaded.comments[0]
    assert c.text == "please use postgres"
    assert c.author == "maxim"
    assert c.kind == "human"
    assert c.ts


def test_add_comment_also_appends_to_context(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    tasks.add_comment(env, t, "please use postgres", author="maxim", kind="human")
    reloaded = tasks.find(env, t.id)
    assert "please use postgres" in reloaded.context
    assert "maxim" in reloaded.context


def test_add_comment_default_author_and_kind(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    tasks.add_comment(env, t, "looks good")
    reloaded = tasks.find(env, t.id)
    assert reloaded.comments[0].author == "user"
    assert reloaded.comments[0].kind == "human"


def test_comments_field_in_to_dict(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    tasks.add_comment(env, t, "note")
    reloaded = tasks.find(env, t.id)
    d = tasks.to_dict(reloaded)
    assert d["comments"][0]["text"] == "note"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k comment`
Expected: FAIL — `AttributeError: module 'harn.tasks' has no attribute 'add_comment'`.

- [ ] **Step 3: Implement**

Add the `Comment` dataclass in `harn/tasks.py` right after `ChangeEntry`:

```python
@dataclass
class Comment:
    """A human (or, later, tracker-sync-imported) comment on a task —
    distinct from `ReviewEntry` (agent lifecycle events) and `Decision`
    (agent claims). `kind` is one of human/hil/agent/external; `hil` is a
    recorded human answer to a blocking question (see loop.answer), kept in
    the same feed so the modal shows one merged conversation instead of
    splitting it across the blocked-question banner and this section.
    """
    ts:     str
    author: str
    text:   str
    kind:   str = "human"

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v}
```

Add `comments: list[Comment] = field(default_factory=list)` to the `Task` dataclass (near `review_log`).

Add `"Comments"` to `_KNOWN_SECTIONS`:

```python
_KNOWN_SECTIONS = ("Description", "Result", "Decisions", "Review log", "Comments", "Changelog")
```

In `_render_header`, add a `## Comments` block right after Review log:

```python
    parts = [_render_frontmatter(task), "", "## Description",
             task.description.strip(), "", "## Result", task.result.strip(),
             "", "## Decisions", _render_bullets(task.decisions),
             "", "## Review log", _render_bullets(task.review_log),
             "", "## Comments", _render_bullets(task.comments),
             "", "## Changelog", _render_bullets(task.changelog),
             "", _CONTEXT_MARKER]
```

In `_build_task`, add comment parsing alongside `review_log`/`decisions`:

```python
    comments = [Comment(**json.loads(ln)) for ln in _bullets(sections.get("Comments", ""))]
```

and pass `comments=comments,` into the returned `Task(...)`.

In `to_dict()`, add:

```python
        "comments":    [c.to_dict() for c in task.comments],
```

Add `add_comment`:

```python
def add_comment(env_dir: Path, task: Task, text: str, *,
                author: str = "user", kind: str = "human") -> None:
    """Post a comment: append it to the task's `## Comments` section AND to
    `## Context` (via `append_context`) so it's a real communication channel
    — the next agent turn actually sees it, not just a UI log."""
    task.comments.append(Comment(ts=_now_iso(), author=author, text=text, kind=kind))
    _save(task)
    append_context(env_dir, task.id, step_id="comment",
                   text=f"[comment by {author}] {text}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k comment`
Expected: 4 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`
Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add harn/tasks.py tests/test_board_kanban.py
git commit -m "feat(tasks): add Comment model, ## Comments section, add_comment()"
```

---

### Task 3: HIL answers recorded as `kind: hil` comments

**Files:**
- Modify: `harn/loop.py` (the `answer()` function, around line 2686-2730)
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: `tasks.add_comment(env_dir, task, text, author=source, kind="hil")` from Task 2.

- [ ] **Step 1: Write the failing test**

```python
def test_hil_answer_recorded_as_comment(tmp_path):
    from harn import loop, state
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    state_dir = env / "state"
    st = state.State(current_task=t.id)
    st.block("which db?")
    st.save(state_dir)
    loop.answer(env, "use postgres", source="telegram")
    reloaded = tasks.find(env, t.id)
    assert any(c.kind == "hil" and c.text == "use postgres" for c in reloaded.comments)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k hil_answer`
Expected: FAIL — `assert False` (no `hil` comment recorded yet).

- [ ] **Step 3: Implement**

In `harn/loop.py`'s `answer()`, the existing block:

```python
    if st.current_task:
        cur_task = tasks.find(env_dir, st.current_task)
        if cur_task and cur_task.step_results:
            changed = False
            for res in cur_task.step_results.values():
                if isinstance(res, dict) and res.get("attempts"):
                    res["attempts"] = 0
                    changed = True
            if changed:
                tasks._save(cur_task)
```

becomes:

```python
    if st.current_task:
        cur_task = tasks.find(env_dir, st.current_task)
        if cur_task:
            changed = False
            for res in cur_task.step_results.values():
                if isinstance(res, dict) and res.get("attempts"):
                    res["attempts"] = 0
                    changed = True
            if changed:
                tasks._save(cur_task)
            tasks.add_comment(env_dir, cur_task, text, author=source, kind="hil")
```

(Note: `add_comment` calls `tasks._save` itself, so the explicit save above only needs to run when attempts were reset; `add_comment` always saves regardless, so ordering is safe either way — the attempts reset is preserved before `add_comment`'s own save.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k hil_answer`
Expected: 1 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`
Expected: all passing (this touches a widely-used function — watch for any test asserting exact `step_results` mutation behavior around the old `if cur_task and cur_task.step_results:` guard change).

- [ ] **Step 6: Commit**

```bash
git add harn/loop.py tests/test_board_kanban.py
git commit -m "feat(loop): record HIL answers as kind=hil task comments"
```

---

### Task 4: `[board] launch_on_drag_in_progress` config setting

**Files:**
- Modify: `harn/config.py`
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Produces: `Config.launch_on_drag_in_progress: bool` (default `False`), parsed from `harn.toml`'s `[board]` section alongside the existing `statuses`/`status` keys from the custom-statuses spec.

- [ ] **Step 1: Write the failing tests**

```python
def test_launch_on_drag_in_progress_defaults_false(tmp_path):
    env = _env(tmp_path)
    cfg = config_mod.Config.load(env)
    assert cfg.launch_on_drag_in_progress is False


def test_launch_on_drag_in_progress_true_from_toml(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text(
        "[board]\nlaunch_on_drag_in_progress = true\n", encoding="utf-8")
    cfg = config_mod.Config.load(env)
    assert cfg.launch_on_drag_in_progress is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k launch_on_drag`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'launch_on_drag_in_progress'`.

- [ ] **Step 3: Implement**

In `harn/config.py`'s `Config` dataclass, add near `board_statuses`:

```python
    # Kanban drag-drop: does dropping a card into in_progress launch a run
    # (today's <select>-driven behavior) or only change status, leaving
    # Launch as an explicit action in the task modal? Default false — a
    # drag should not have a launch side effect unless a project opts in.
    launch_on_drag_in_progress: bool = False
```

In `Config.load()`'s return, add:

```python
            launch_on_drag_in_progress=bool(
                (data.get("board", {}) or {}).get("launch_on_drag_in_progress", False)
            ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k launch_on_drag`
Expected: 2 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/config.py tests/test_board_kanban.py
git commit -m "feat(config): add [board] launch_on_drag_in_progress setting"
```

---

### Task 5: `studio.update_task_payload` (partial task update)

**Files:**
- Modify: `harn/studio.py`
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: `tasks.find`, `tasks._save`, `workflows.load`/`workflows.list_names` pattern already used by `set_task_workflow` (for validating a `workflow` field, if supplied — reuse `set_task_workflow`'s existing validation logic rather than duplicating it: call it internally when `workflow` is present in the payload).
- Produces: `update_task_payload(env_dir, payload) -> dict` — `{"ok": True, "task_id": ...}` or `{"ok": False, "error": ...}`. Only keys present in `payload` among `title`/`description`/`priority`/`workflow`/`labels` are applied; `status` is rejected with a clear error if present (status changes stay on `/api/tasks/status`).

- [ ] **Step 1: Write the failing tests**

```python
def test_update_task_payload_changes_only_submitted_fields(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Original title", description="orig desc", priority=10)
    r = studio.update_task_payload(env, {"task_id": t.id, "title": "New title"})
    assert r["ok"] is True
    reloaded = tasks.find(env, t.id)
    assert reloaded.title == "New title"
    assert reloaded.description == "orig desc"   # untouched
    assert reloaded.priority == 10                # untouched


def test_update_task_payload_sets_labels(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    r = studio.update_task_payload(env, {"task_id": t.id, "labels": ["bug", "urgent"]})
    assert r["ok"] is True
    assert tasks.find(env, t.id).labels == ["bug", "urgent"]


def test_update_task_payload_rejects_status_field(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    r = studio.update_task_payload(env, {"task_id": t.id, "status": "review"})
    assert r["ok"] is False
    assert "status" in r["error"].lower()
    assert tasks.find(env, t.id).status == tasks.TODO


def test_update_task_payload_unknown_task(tmp_path):
    env = _env(tmp_path)
    r = studio.update_task_payload(env, {"task_id": "NOPE", "title": "x"})
    assert r["ok"] is False


def test_update_task_payload_priority_coerced_to_int(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    r = studio.update_task_payload(env, {"task_id": t.id, "priority": "5"})
    assert r["ok"] is True
    assert tasks.find(env, t.id).priority == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k update_task_payload`
Expected: FAIL — `AttributeError: module 'harn.studio' has no attribute 'update_task_payload'`.

- [ ] **Step 3: Implement**

In `harn/studio.py`, add near `set_task_workflow`:

```python
def update_task_payload(env_dir: Path, payload: dict) -> dict:
    """Partial task update — title/description/priority/workflow/labels.
    Deliberately does NOT accept `status`: status changes (including drag)
    stay on set_task_status_payload/`/api/tasks/status` so exactly one code
    path ever mutates it."""
    task_id = (payload.get("task_id") or "").strip()
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    if "status" in payload:
        return {"ok": False, "error": "status is not editable here — use /api/tasks/status"}
    if "title" in payload:
        title = (payload.get("title") or "").strip()
        if not title:
            return {"ok": False, "error": "title cannot be empty"}
        task.title = title
    if "description" in payload:
        task.description = payload.get("description") or ""
    if "priority" in payload:
        try:
            task.priority = int(payload.get("priority"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "priority must be a number"}
    if "labels" in payload:
        raw = payload.get("labels") or []
        task.labels = [str(x).strip() for x in raw if str(x).strip()]
    if "workflow" in payload:
        wf_result = set_task_workflow(env_dir, {"task_id": task_id, "workflow": payload.get("workflow") or ""})
        if not wf_result.get("ok"):
            return wf_result
        task = tasks_mod.find(env_dir, task_id)   # set_task_workflow already saved
    tasks_mod._save(task)
    return {"ok": True, "task_id": task_id}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k update_task_payload`
Expected: 5 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_board_kanban.py
git commit -m "feat(studio): add update_task_payload for partial task edits"
```

---

### Task 6: `POST /api/tasks/update` route + `POST /api/tasks/comment` route

**Files:**
- Modify: `harn/studio.py`
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: `update_task_payload` (Task 5), `tasks.add_comment` (Task 2).
- Produces: `add_comment_payload(env_dir, payload) -> dict`; two new `elif route == ...` branches in `do_POST`.

- [ ] **Step 1: Write the failing tests**

```python
def test_add_comment_payload_posts_a_comment(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    r = studio.add_comment_payload(env, {"task_id": t.id, "text": "looks good"})
    assert r["ok"] is True
    reloaded = tasks.find(env, t.id)
    assert reloaded.comments[0].text == "looks good"
    assert reloaded.comments[0].kind == "human"


def test_add_comment_payload_rejects_empty_text(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    r = studio.add_comment_payload(env, {"task_id": t.id, "text": "  "})
    assert r["ok"] is False


def test_add_comment_payload_unknown_task(tmp_path):
    env = _env(tmp_path)
    r = studio.add_comment_payload(env, {"task_id": "NOPE", "text": "hi"})
    assert r["ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k add_comment_payload`
Expected: FAIL — `AttributeError: module 'harn.studio' has no attribute 'add_comment_payload'`.

- [ ] **Step 3: Implement**

In `harn/studio.py`, add near `update_task_payload`:

```python
def add_comment_payload(env_dir: Path, payload: dict) -> dict:
    task_id = (payload.get("task_id") or "").strip()
    text = (payload.get("text") or "").strip()
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    if not text:
        return {"ok": False, "error": "comment text cannot be empty"}
    tasks_mod.add_comment(env_dir, task, text, author="user", kind="human")
    return {"ok": True, "task_id": task_id}
```

In `do_POST`'s route chain, add right after the `/api/tasks/workflow` branch:

```python
            elif route == "/api/tasks/update":
                self._json(update_task_payload(env, body))
            elif route == "/api/tasks/comment":
                self._json(add_comment_payload(env, body))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k add_comment_payload`
Expected: 3 passed.

- [ ] **Step 5: Full suite regression check + entire new test file**

Run: `python3 -m pytest tests/test_board_kanban.py -v && python3 -m pytest -q`
Expected: all passing (~17 new tests in test_board_kanban.py from Tasks 1-6 combined; full suite green).

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_board_kanban.py
git commit -m "feat(studio): add /api/tasks/update and /api/tasks/comment routes"
```

---

### Task 7: Board columns render for every configured status (including empty)

**Files:**
- Modify: `harn/studio.py` (the `_HTML` template's `renderBoard()`, around line 2318-2345)
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: `BOARD_ORDER` (already payload-driven per the custom-statuses spec's `applyBoardStatuses`).
- Produces: a `renderBoard()` that emits one `.kanban-col` div per `BOARD_ORDER` entry unconditionally (not `if(!list.length) return;`), each with a header (label + count) and a `data-status="<status>"` attribute (the drop-target hook Task 8 needs).

This task is UI-structural; verified two ways: (a) a Python regression test asserting the served `_HTML` string contains the right markers, matching the existing pattern (`test_studio_html_renders_board_order_as_javascript_var` in `tests/test_board_statuses.py`), and (b) manual/Playwright browser verification at the end of Task 10 once drag-drop is wired (a column with zero cards is hard to usefully assert via Playwright in isolation before drag exists — deferred to the combined browser check).

- [ ] **Step 1: Write the failing test**

```python
def test_studio_html_renders_kanban_columns_unconditionally():
    assert "kanban-col" in studio._HTML
    assert "data-status=" in studio._HTML
    # The old early-return-on-empty-column guard must be gone.
    assert "if(!list.length) return;" not in studio._HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k kanban_columns`
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace the body of `renderBoard()` in `harn/studio.py`'s `_HTML` (the `BOARD_ORDER.forEach(s=>{...})` block) with a version that renders every column unconditionally, wraps them in a horizontally-scrolling container, and adds `data-status` for drop targeting:

```javascript
function renderBoard(){
  const v=$('#listView');
  const groups={}; (BOARD.tasks||[]).forEach(t=>(groups[t.status]=groups[t.status]||[]).push(t));
  let html='<h2>BOARD</h2>'+
    '<button class="ghost" onclick="showNewTaskForm()" style="margin-bottom:8px">＋ New task</button>'+
    `<div id="newTaskForm" style="display:${NEW_TASK_OPEN?'block':'none'}"></div>`;
  if(BOARD.run){
    const rt=(BOARD.tasks||[]).find(t=>t.id===BOARD.run.task_id);
    html+=`<div class="runbanner">▶ running <b>${esc(BOARD.run.task_id)}</b>`+
      `${rt?': '+esc(rt.title):''} (pid ${BOARD.run.pid}${BOARD.run.auto?' · auto':''})`+
      `<button class="ghost" onclick="stopRun()" title="Steps already done stay done; edit the plan, then ▶ Resume">⏸ Pause</button></div>`;
  }
  html+='<div class="kanban-board">';
  BOARD_ORDER.forEach(s=>{
    const list=(groups[s]||[]).slice().sort((a,b)=>a.priority-b.priority);
    html+=`<div class="kanban-col" data-status="${esc(s)}" ondragover="onColDragOver(event)" ondrop="onColDrop(event,'${esc(s)}')">`+
      `<div class="kanban-col-head">${BOARD_LABEL[s]||s} · ${list.length}</div>`+
      `<div class="kanban-col-body">`;
    list.forEach(t=>{
      const running=BOARD.run&&BOARD.run.task_id===t.id;
      const draggable=!running;
      html+=`<div class="kanban-card ${boardSel===t.id?'sel':''} ${running?'running':''}" `+
        `draggable="${draggable}" ondragstart="onCardDragStart(event,'${esc(t.id)}')" ondragend="onCardDragEnd(event)" `+
        `onclick="openTaskModal('${esc(t.id)}')">`+
        `<div class="kanban-card-title">${esc(t.id)}: ${esc(t.title)}${running?'<span class="live-dot" title="running"></span>':''}</div>`+
        `<div class="kanban-card-labels">${(t.labels||[]).map(l=>`<span class="chip">${esc(l)}</span>`).join('')}</div>`+
        `<div class="ds">${esc(t.workflow||'default workflow')} · priority ${t.priority}${t.claimed_by?' · '+esc(t.claimed_by):''}</div>`+
        `</div>`;
    });
    html+='</div></div>';
  });
  html+='</div>';
  if(!(BOARD.tasks||[]).length) html+='<div class="empty">No tasks yet — create one from an agent session (create_task).</div>';
  v.innerHTML=html;
  BOARD_LIST_RENDER_KEY=boardListRenderKey();
}
```

(`onColDragOver`/`onColDrop`/`onCardDragStart`/`onCardDragEnd`/`openTaskModal` are stubbed as no-ops for now — implemented in Tasks 8 and 9. Add the minimal stubs right after `renderBoard` so the page doesn't throw:)

```javascript
function onColDragOver(e){ e.preventDefault(); }
function onCardDragStart(e,id){}
function onCardDragEnd(e){}
function onColDrop(e,status){ e.preventDefault(); }
function openTaskModal(id){ selectTask(id); }   // Task 9 replaces this with a real modal
```

Also add minimal CSS for `.kanban-board`/`.kanban-col`/`.kanban-card` in the existing `<style>` block (horizontal flex row, fixed-width scrollable columns, vertical scroll per column) — follow the existing CSS variable conventions (`var(--panel)`, `var(--line)`, etc.) already used throughout `_HTML`'s `<style>`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k kanban_columns`
Expected: 1 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`
Expected: all passing — this only touches the `_HTML` string, no Python logic changed, so no existing test should break; if `test_board.py`/`test_studio_models.py` assert anything about the old `.boardgroup`/`if(!list.length) return;` markup, update those assertions to match (check via `grep -rn "boardgroup" tests/`).

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_board_kanban.py
git commit -m "feat(studio): render Kanban columns for every configured status"
```

---

### Task 8: Drag-and-drop card → status change (optimistic + rollback + DRAGGING guard)

**Files:**
- Modify: `harn/studio.py` (`_HTML`'s JS: `pollBoard`, the drag stub functions from Task 7, plus a new `DRAGGING` flag)
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: `POST /api/tasks/status` (existing, unchanged), `MODELS`/`BOARD` globals.
- Produces: `onCardDragStart`/`onCardDragEnd`/`onColDrop` fully implemented; `pollBoard()` skips its render while `DRAGGING` is true; a `RUN_CAPS`-style module global `DRAGGING=false`.

- [ ] **Step 1: Write the failing test**

```python
def test_studio_html_has_dragging_guard_in_poll_board():
    assert "let DRAGGING" in studio._HTML or "var DRAGGING" in studio._HTML
    assert "onColDrop" in studio._HTML
    assert "/api/tasks/status" in studio._HTML   # drop still uses the existing route
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k dragging_guard`
Expected: FAIL (the `DRAGGING` var doesn't exist yet — Task 7 only stubbed the handlers).

- [ ] **Step 3: Implement**

Add near the other module-level JS globals (alongside `NEW_TASK_OPEN`):

```javascript
let DRAGGING=false, DRAG_TASK_ID=null;
```

Replace the Task 7 stubs with real implementations:

```javascript
function onCardDragStart(e,id){
  DRAGGING=true; DRAG_TASK_ID=id;
  e.dataTransfer.effectAllowed='move';
  e.dataTransfer.setData('text/plain',id);
}
function onCardDragEnd(e){
  DRAGGING=false; DRAG_TASK_ID=null;
}
function onColDragOver(e){ e.preventDefault(); e.dataTransfer.dropEffect='move'; }
async function onColDrop(e,status){
  e.preventDefault();
  const taskId=DRAG_TASK_ID || e.dataTransfer.getData('text/plain');
  DRAGGING=false; DRAG_TASK_ID=null;
  if(!taskId) return;
  const t=(BOARD.tasks||[]).find(x=>x.id===taskId);
  if(!t || t.status===status) return;
  const priorStatus=t.status;
  t.status=status;             // optimistic move
  renderBoard();
  const r=await post_('/api/tasks/status',{task_id:taskId,status});
  if(!r.ok){
    t.status=priorStatus;      // roll back
    renderBoard();
    alert(r.error||'status change failed');
    return;
  }
  await pollBoard();
}
```

In `pollBoard()`, guard the render:

```javascript
async function pollBoard(){
  const generation=++boardPollGeneration;
  let next;
  try{ next=await (await fetch(api('/api/board'))).json(); }catch(e){ return; }
  if(generation!==boardPollGeneration) return;
  BOARD=next;
  applyBoardStatuses(BOARD.statuses);
  if(BOARD.run&&BOARD.run.task_id) LAST_RUN_TASK=BOARD.run.task_id;
  await pollBlockedQuestion();
```

— find the line that currently calls `renderBoard()` unconditionally after this block (search for where `pollBoard` triggers a re-render; it may be implicit via a shared render-key check) and wrap it: `if(!DRAGGING) renderBoard();` (or the equivalent existing render-dispatch call — inspect the surrounding code at implementation time to match the exact existing call site rather than assuming its name).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k dragging_guard`
Expected: 1 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_board_kanban.py
git commit -m "feat(studio): drag-and-drop card status change with optimistic update"
```

---

### Task 9: `launch_on_drag_in_progress` gating on drop into `in_progress`

**Files:**
- Modify: `harn/studio.py` (Python: `board_payload` ships the setting; JS: `onColDrop`)
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: `Config.launch_on_drag_in_progress` (Task 4).
- Produces: the drop always POSTs `/api/tasks/status` with a `source: 'drag'` marker; `set_task_status_payload` gates its existing launch-and-rollback behavior on that marker + the setting. `board_payload` does NOT need to ship the setting to the client — the gate lives entirely server-side, so the client never has to know its value.

`set_task_status_payload`'s current body (confirmed by reading `harn/studio.py`) ALWAYS attempts a launch on any transition to `in_progress`, with no way today to tell "the `<select>` picked this" apart from "a drag dropped this" — both are the same POST. The fix adds one optional `source` field to the request body so the two call sites can be told apart, and gates the launch on it.

- [ ] **Step 1: Write the failing tests**

```python
def test_drag_drop_into_in_progress_only_launches_when_setting_enabled(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})
    launched = {"n": 0}
    def fake_launch(pr, ed, task_id, *, auto=False):
        launched["n"] += 1
        return {"ok": True, "pid": 1, "task_id": task_id, "auto": auto}
    monkeypatch.setattr(studio.runner_mod, "launch", fake_launch)

    r = studio.set_task_status_payload(
        env, {"task_id": t.id, "status": "in_progress", "source": "drag"})
    assert r["ok"] is True
    assert launched["n"] == 0          # default false: no launch
    assert tasks.find(env, t.id).status == "in_progress"

    (env / "harn.toml").write_text(
        "[board]\nlaunch_on_drag_in_progress = true\n", encoding="utf-8")
    t2 = tasks.create_task(env, "T2")
    studio.set_task_workflow(env, {"task_id": t2.id, "workflow": ""})
    r2 = studio.set_task_status_payload(
        env, {"task_id": t2.id, "status": "in_progress", "source": "drag"})
    assert r2["ok"] is True
    assert launched["n"] == 1          # now enabled: launches


def test_dropdown_status_change_still_launches_unconditionally(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})
    launched = {"n": 0}
    monkeypatch.setattr(studio.runner_mod, "launch",
                        lambda pr, ed, tid, *, auto=False: launched.update(n=1) or
                        {"ok": True, "pid": 1, "task_id": tid, "auto": auto})
    r = studio.set_task_status_payload(env, {"task_id": t.id, "status": "in_progress"})
    assert r["ok"] is True
    assert launched["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k "drag_drop_into_in_progress or dropdown_status_change"`
Expected: FAIL — `test_drag_drop_into_in_progress_only_launches_when_setting_enabled` fails at `assert launched["n"] == 0` (today's code launches unconditionally, ignoring `source`).

- [ ] **Step 3: Implement**

In `set_task_status_payload` (`harn/studio.py`), change the `if new_status == tasks_mod.IN_PROGRESS:` condition to also check the setting when the request came from a drag:

```python
    if new_status == tasks_mod.IN_PROGRESS and (
        payload.get("source") != "drag" or Config.load(env_dir).launch_on_drag_in_progress
    ):
        # existing launch-and-rollback body, unchanged
        ...
    else:
        tasks_mod.set_status(task, new_status, env_dir)
        return {"ok": True, "task_id": task_id, "status": new_status}
```

Then in the JS `onColDrop`, send `source: 'drag'` on every drop:

```javascript
  const r=await post_('/api/tasks/status',{task_id:taskId,status,source:'drag'});
```

(the dropdown's `changeTaskStatus` stays as-is, with no `source` field, so it keeps launching unconditionally — matching today's behavior exactly).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k "drag_drop_into_in_progress or dropdown_status_change"`
Expected: 2 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`
Expected: all passing — the changed condition in `set_task_status_payload` must not alter behavior for every existing caller that doesn't pass `source` (verify `tests/test_board.py`'s existing status-payload tests, e.g. `test_status_payload_launches_a_run_once_flow_confirmed`, still pass unchanged since they never pass `source`).

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_board_kanban.py
git commit -m "feat(studio): gate drag-into-in_progress launch behind a setting"
```

---

### Task 10: Task detail modal (structure + open/close), replacing the side panel

**Files:**
- Modify: `harn/studio.py` (`_HTML`: new modal container in the page shell, `openTaskModal`/`closeTaskModal`, relocate `renderTaskDetail`'s output target from `#insp` to the modal body)
- Test: `tests/test_board_kanban.py`

**Interfaces:**
- Consumes: `renderTaskDetail()` (existing function, content mostly unchanged — Tasks 11-12 add editable fields and Comments to its output).
- Produces: `<div id="taskModal">` in the static page shell (hidden by default), `openTaskModal(id)` (sets `boardSel`, shows the modal, calls `renderTaskDetail()`), `closeTaskModal()` (hides it, does NOT clear `boardSel` — so re-opening the same task doesn't lose scroll-position logic already in `renderTaskDetail`), Esc-key and backdrop-click close handlers.

- [ ] **Step 1: Write the failing test**

```python
def test_studio_html_has_task_modal_shell():
    assert 'id="taskModal"' in studio._HTML
    assert "function openTaskModal" in studio._HTML
    assert "function closeTaskModal" in studio._HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k task_modal_shell`
Expected: FAIL.

- [ ] **Step 3: Implement**

Find the static page shell in `_HTML` (the HTML markup section before the `<script>` block, where `#insp` currently sits as a panel). Add a modal container as a sibling near it:

```html
<div id="taskModal" class="modal-backdrop" style="display:none" onclick="if(event.target===this) closeTaskModal()">
  <div class="modal-panel" id="insp"></div>
</div>
```

(Reusing `id="insp"` as the modal's inner content target means `renderTaskDetail()` needs zero changes to where it writes — it already does `const panel=$('#insp'); ... panel.innerHTML=...`. This is the minimal-diff approach: only the container around `#insp` changes from an always-visible side panel to a hidden-by-default modal backdrop.)

Add CSS for `.modal-backdrop` (fixed, full-viewport, semi-transparent dark background, centered flex) and `.modal-panel` (the existing `#insp` panel styling, now inside a centered, max-width, scrollable box) to the `<style>` block — adapt whatever CSS rules currently target `#insp`'s panel appearance (find them via `grep -n "#insp" harn/studio.py` at implementation time) rather than duplicating; move/extend them under `.modal-panel`.

Add the JS functions:

```javascript
function openTaskModal(id){
  selectTask(id);
  $('#taskModal').style.display='flex';
  document.addEventListener('keydown', _modalEscHandler);
}
function closeTaskModal(){
  $('#taskModal').style.display='none';
  document.removeEventListener('keydown', _modalEscHandler);
}
function _modalEscHandler(e){ if(e.key==='Escape') closeTaskModal(); }
```

Replace the Task 7 stub `function openTaskModal(id){ selectTask(id); }` with this real version (it now also shows the modal).

Add a close button inside `renderTaskDetail()`'s rendered markup — find the existing header row (`<h2 style="margin:0">${esc(t.id)}</h2>`) and add a close button beside it:

```javascript
      <button class="icon-btn" onclick="closeTaskModal()" title="Close">✕</button>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_board_kanban.py -v -k task_modal_shell`
Expected: 1 passed.

- [ ] **Step 5: Full suite regression check**

Run: `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_board_kanban.py
git commit -m "feat(studio): task detail as a modal instead of a persistent side panel"
```

---

### Task 11: Editable fields in the modal (title/description/priority/workflow/labels)

**Files:**
- Modify: `harn/studio.py` (`renderTaskDetail()`'s markup + new `saveTaskField`/`saveTaskLabels` JS functions)
- Test: manual/Playwright only (pure JS wiring over Task 5's already-tested backend route) — no new Python test needed here since `update_task_payload` is fully covered by Task 5; this task only wires the UI to call it.

**Interfaces:**
- Consumes: `POST /api/tasks/update` (Task 6).

- [ ] **Step 1: Implement (no separate failing-test step — this is UI wiring over an already-tested route; verify via the Task 5 backend tests staying green plus the final Task 13 browser check)**

In `renderTaskDetail()`, change the static title/description display into editable controls:

```javascript
    <input type="text" id="taskTitleInput" class="taskTitle" value="${esc(t.title)}"
      onblur="saveTaskField('${esc(t.id)}','title',this.value)"/>
    ...
    <label>Description</label>
    <textarea id="taskDescInput" rows="4" class="toolDoc"
      onblur="saveTaskField('${esc(t.id)}','description',this.value)">${esc(t.description||'')}</textarea>
    <label>Priority</label>
    <input type="number" id="taskPriorityInput" value="${t.priority}"
      onblur="saveTaskField('${esc(t.id)}','priority',this.value)"/>
    <label>Labels <span class="mut">(comma-separated)</span></label>
    <input type="text" id="taskLabelsInput" value="${esc((t.labels||[]).join(', '))}"
      onblur="saveTaskLabels('${esc(t.id)}',this.value)"/>
```

(Replace the existing static `<div class="taskTitle">${esc(t.title)}</div>` and `<div class="toolDoc">${esc(t.description||'(none)')}</div>` lines with the above; `workflow` is already editable via the existing `assignWorkflow` select — no change needed there, it already calls `/api/tasks/workflow` directly, which Task 5's `update_task_payload` also delegates to for consistency but doesn't need to replace.)

Add the JS handlers:

```javascript
async function saveTaskField(taskId,field,value){
  const body={task_id:taskId}; body[field]=value;
  const r=await post_('/api/tasks/update',body);
  if(!r.ok){ alert(r.error||'update failed'); }
  await pollBoard();
}
async function saveTaskLabels(taskId,csv){
  const labels=csv.split(',').map(s=>s.trim()).filter(Boolean);
  const r=await post_('/api/tasks/update',{task_id:taskId,labels});
  if(!r.ok){ alert(r.error||'update failed'); }
  await pollBoard();
}
```

- [ ] **Step 2: Full suite regression check**

Run: `python3 -m pytest -q`
Expected: all passing (no Python logic changed).

- [ ] **Step 3: Commit**

```bash
git add harn/studio.py
git commit -m "feat(studio): editable title/description/priority/labels in task modal"
```

---

### Task 12: Comments/Activity feed in the modal

**Files:**
- Modify: `harn/studio.py` (`renderTaskDetail()`'s markup, new `postComment` JS function)
- Test: none new (backend already covered by Task 6) — UI wiring only, verified in Task 13's browser check.

**Interfaces:**
- Consumes: `t.comments` (from `board_payload`, Task 2's `to_dict` addition) and `t.review_log` (existing), merged and sorted by `ts`; `POST /api/tasks/comment` (Task 6).

- [ ] **Step 1: Implement**

In `renderTaskDetail()`, after the existing `Review log` section, add a merged feed. Build it as a small helper the render function calls:

```javascript
function renderActivityFeed(t){
  const items=[
    ...(t.review_log||[]).map(e=>({ts:e.ts,kind:'agent',
      text:`${e.event}${e.by?' by '+e.by:e.agent?' ('+e.agent+')':''}${e.summary?': '+e.summary:''}${e.comment?': '+e.comment:''}${e.notes?' — '+e.notes:''}`})),
    ...(t.comments||[]).map(c=>({ts:c.ts,kind:c.kind,author:c.author,text:c.text})),
  ].sort((a,b)=>(a.ts||'').localeCompare(b.ts||''));
  if(!items.length) return '<span class="mut">no activity yet</span>';
  const KIND_BADGE={agent:'🤖',human:'💬',hil:'📩',external:'🔗'};
  return items.map(i=>
    `<div class="activity-item"><span class="kind-badge" title="${esc(i.kind)}">${KIND_BADGE[i.kind]||'•'}</span>`+
    `<span class="mut" style="font-size:11px">${esc(i.ts||'')}${i.author?' · '+esc(i.author):''}</span>`+
    `<div>${esc(i.text)}</div></div>`
  ).join('');
}
```

Replace the existing `Review log` section's markup:

```javascript
    <label>Review log</label>
    <div class="toolDoc" id="reviewLog" style="max-height:160px;overflow:auto;user-select:text">${esc(reviewLog)}</div>
```

with:

```javascript
    <label>Activity</label>
    <div class="toolDoc" id="reviewLog" style="max-height:220px;overflow:auto;user-select:text">${renderActivityFeed(t)}</div>
    <div class="row" style="gap:6px;margin-top:6px">
      <input type="text" id="commentInput" placeholder="Add a comment…" style="flex:1"/>
      <button class="ghost" onclick="postComment('${esc(t.id)}')">Post</button>
    </div>
```

(Keep the `id="reviewLog"` on the new container — the existing scroll-position-preservation code in `renderTaskDetail` already reads/restores `$('#reviewLog').scrollTop`, so reusing the id avoids touching that logic.)

Remove the now-redundant `reviewLog` plain-text variable computation (the `const reviewLog = (t.review_log||[]).map(...)` line) since `renderActivityFeed` supersedes it — or leave it unused only if nothing else references it; check with `grep -n "reviewLog" harn/studio.py` and delete the dead variable if unused elsewhere.

Add the JS handler:

```javascript
async function postComment(taskId){
  const box=$('#commentInput');
  const text=box?box.value.trim():'';
  if(!text) return;
  const r=await post_('/api/tasks/comment',{task_id:taskId,text});
  if(!r.ok){ alert(r.error||'comment failed'); return; }
  if(box) box.value='';
  await pollBoard();
}
```

- [ ] **Step 2: Full suite regression check**

Run: `python3 -m pytest -q`

- [ ] **Step 3: Commit**

```bash
git add harn/studio.py
git commit -m "feat(studio): merged Comments/Activity feed in the task modal"
```

---

### Task 13: Browser verification (Playwright)

**Files:** none (verification only).

- [ ] **Step 1:** Start Studio against a scratch project (`harn setup` in a tmp dir, `harn ui`).
- [ ] **Step 2:** Create 3-4 tasks across different statuses (via the "＋ New task" form and drag).
- [ ] **Step 3:** Verify: all configured columns render, including at least one empty column.
- [ ] **Step 4:** Drag a card from `todo` to `review` — verify the card moves and the status persists after a page reload.
- [ ] **Step 5:** With `launch_on_drag_in_progress` left at its default (`false`), drag a card into `in_progress` — verify NO run starts (no run banner appears) and the task's status is `in_progress`.
- [ ] **Step 6:** Set `[board] launch_on_drag_in_progress = true` in `harn.toml`, reload, drag a card (with a confirmed workflow) into `in_progress` — verify a run launches (run banner appears).
- [ ] **Step 7:** Click a card — verify the modal opens (not a side panel), shows description/labels/metadata/attachments/pipeline/activity.
- [ ] **Step 8:** Edit the title and priority in the modal, click elsewhere (blur) — verify the card's title updates on the board without a manual refresh (next `pollBoard` tick).
- [ ] **Step 9:** Add a label via the labels input — verify the chip appears on the card.
- [ ] **Step 10:** Post a comment — verify it appears in the Activity feed immediately, and that a subsequent `read_context`-style check (or a raw look at the task's `.md` file) shows it landed in `## Comments` and `## Context`.
- [ ] **Step 11:** Trigger a HIL block (or simulate via `loop.answer` in a scratch env) and confirm the answer shows up in the same Activity feed tagged `hil`.
- [ ] **Step 12:** Close the modal via Esc, via backdrop click, and via the close button — all three work.
- [ ] **Step 13:** Take a screenshot of the populated board and the open modal; report both to the user as visual proof (per this project's UI-verification convention — do not claim success without this).

No commit for this task (verification-only); if any step surfaces a bug, fix it in the relevant earlier task's files and re-run that task's tests plus the full suite before re-verifying.
