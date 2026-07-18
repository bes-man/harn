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
