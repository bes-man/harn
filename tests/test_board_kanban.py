"""Studio board Kanban redesign (docs/superpowers/specs/2026-07-18-studio-board-kanban-design.md):
labels, comments, partial task updates, drag-triggered launch gating."""
from __future__ import annotations

from harn import config as config_mod
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
