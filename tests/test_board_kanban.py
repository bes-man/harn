"""Studio board Kanban redesign (docs/superpowers/specs/2026-07-18-studio-board-kanban-design.md):
labels, comments, partial task updates, drag-triggered launch gating."""
from __future__ import annotations

from harn import studio, tasks, workflows, ENV_DIRNAME


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


def test_update_task_payload_combines_workflow_with_other_fields(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    workflows.create(env, name="qa", title="QA")
    r = studio.update_task_payload(env, {"task_id": t.id, "title": "New Title", "workflow": "qa"})
    assert r["ok"] is True
    reloaded = tasks.find(env, t.id)
    assert reloaded.title == "New Title"
    assert reloaded.workflow == "qa"


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


def test_studio_html_renders_kanban_columns_unconditionally():
    assert "kanban-col" in studio._HTML
    assert "data-status=" in studio._HTML
    # The old early-return-on-empty-column guard must be gone.
    assert "if(!list.length) return;" not in studio._HTML


def test_studio_html_has_dragging_guard_in_poll_board():
    assert "let DRAGGING" in studio._HTML or "var DRAGGING" in studio._HTML
    assert "onColDrop" in studio._HTML
    assert "/api/tasks/status" in studio._HTML   # drop still uses the existing route


def test_drag_drop_into_in_progress_launches_the_same_as_the_dropdown(tmp_path, monkeypatch):
    """A card's launch behavior must not depend on HOW it arrived on a
    column — drag and the status dropdown go through the exact same route
    and must produce the exact same effect. `launch_on_drag_in_progress`
    (a separate opt-in just for drag) was removed for exactly this reason:
    a column either dispatches on arrival or it doesn't, regardless of
    gesture."""
    env = _env(tmp_path)
    t = tasks.create_task(env, "T")
    studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})
    launched = {"n": 0}
    def fake_launch(pr, ed, task_id, *, auto=False, step=None, rerun=False, as_role=None):
        launched["n"] += 1
        return {"ok": True, "pid": 1, "task_id": task_id, "auto": auto}
    monkeypatch.setattr(studio.runner_mod, "launch", fake_launch)

    r = studio.set_task_status_payload(env, {"task_id": t.id, "status": "in_progress"})
    assert r["ok"] is True and r["launched"] is True
    assert launched["n"] == 1
    assert tasks.find(env, t.id).status == "in_progress"


def test_dropping_onto_a_column_with_an_owning_agent_dispatches_that_agent(tmp_path, monkeypatch):
    """The general case this replaces the in_progress special-case with: ANY
    column with an owning role dispatches that role the moment a task lands
    on it — via `as_role`, so the run claims the task and picks up the
    role's own workflow, the same as a Telegram /command would."""
    env = _env(tmp_path)
    (env / "agents").mkdir(parents=True, exist_ok=True)
    from harn import roles
    roles.save(env, {"name": "reviewer", "command": "review", "status": "review",
                     "trigger": "manual"})
    t = tasks.create_task(env, "Needs review")

    seen = {}
    def fake_launch(pr, ed, task_id, *, auto=False, step=None, rerun=False, as_role=None):
        seen["as_role"] = as_role
        return {"ok": True, "pid": 1, "task_id": task_id, "auto": auto}
    monkeypatch.setattr(studio.runner_mod, "launch", fake_launch)

    r = studio.set_task_status_payload(env, {"task_id": t.id, "status": "review"})

    assert r["ok"] is True and r["launched"] is True
    assert seen["as_role"] == "reviewer"
    assert tasks.find(env, t.id).status == "review"


def test_column_owner_dispatch_ignores_workflow_confirmation(tmp_path, monkeypatch):
    """A role-owned column assigns its OWN workflow on the way in
    (roles_runner.run_role) — it must not be gated behind
    `workflow_confirmed`, which only guards the no-owner in_progress path."""
    env = _env(tmp_path)
    (env / "agents").mkdir(parents=True, exist_ok=True)
    from harn import roles
    roles.save(env, {"name": "reviewer", "command": "review", "status": "review",
                     "trigger": "manual"})
    t = tasks.create_task(env, "Needs review")
    assert t.workflow_confirmed is False

    monkeypatch.setattr(studio.runner_mod, "launch",
                        lambda *a, **k: {"ok": True, "pid": 1, "task_id": t.id})
    r = studio.set_task_status_payload(env, {"task_id": t.id, "status": "review"})
    assert r["ok"] is True


def test_a_refused_column_owner_launch_rolls_the_status_back(tmp_path, monkeypatch):
    env = _env(tmp_path)
    (env / "agents").mkdir(parents=True, exist_ok=True)
    from harn import roles
    roles.save(env, {"name": "reviewer", "command": "review", "status": "review",
                     "trigger": "manual"})
    t = tasks.create_task(env, "Needs review")

    monkeypatch.setattr(studio.runner_mod, "launch",
                        lambda *a, **k: {"ok": False, "error": "busy"})
    r = studio.set_task_status_payload(env, {"task_id": t.id, "status": "review"})

    assert r["ok"] is False
    assert tasks.find(env, t.id).status == "todo"     # rolled back, not stranded


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


def test_studio_html_has_task_modal_shell():
    assert 'id="taskModal"' in studio._HTML
    assert "function openTaskModal" in studio._HTML
    assert "function closeTaskModal" in studio._HTML
