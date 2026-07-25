"""Custom board statuses spec (docs/superpowers/specs/2026-07-18-custom-board-statuses-design.md):
`[board]` config parsing, `tasks.lifecycle`, `set_status` validation against
the configured pipeline, removed-status tasks staying readable, and the
`external` frontmatter field."""
from __future__ import annotations

from harn import config as config_mod
from harn import studio, tasks, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


def _write_toml(env, text):
    (env / "harn.toml").write_text(text, encoding="utf-8")


# --- config parsing --------------------------------------------------- #

def test_no_config_yields_empty_board_statuses(tmp_path):
    env = _env(tmp_path)
    cfg = config_mod.Config.load(env)
    assert cfg.board_statuses == []


def test_simple_statuses_list_form(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "analyzed", "developing", "done"]\n')
    cfg = config_mod.Config.load(env)
    assert [s["name"] for s in cfg.board_statuses] == \
        ["new", "analyzing", "analyzed", "developing", "done"]
    assert all(s["external"] is None for s in cfg.board_statuses)


def test_status_table_form_with_external_metadata(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, (
        '[[board.status]]\nname = "new"\n\n'
        '[[board.status]]\nname = "analyzing"\nexternal = "In Analysis"\n\n'
        '[[board.status]]\nname = "done"\n'
    ))
    cfg = config_mod.Config.load(env)
    assert [s["name"] for s in cfg.board_statuses] == ["new", "analyzing", "done"]
    assert cfg.board_statuses[1]["external"] == "In Analysis"
    assert cfg.board_statuses[0]["external"] is None


# --- tasks.lifecycle ---------------------------------------------------- #

def test_lifecycle_falls_back_to_default_with_no_config(tmp_path):
    env = _env(tmp_path)
    assert tasks.lifecycle(env) == tasks.LIFECYCLE


def test_lifecycle_none_env_dir_falls_back_to_default():
    assert tasks.lifecycle(None) == tasks.LIFECYCLE


def test_lifecycle_reflects_custom_config(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "developing", "done"]\n')
    assert tasks.lifecycle(env) == ["new", "analyzing", "developing", "done"]


# --- set_status validation ---------------------------------------------- #

def test_set_status_validates_against_default_lifecycle_without_env_dir(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    tasks.set_status(t, tasks.REVIEW)
    assert t.status == tasks.REVIEW
    try:
        tasks.set_status(t, "analyzing")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_status_validates_against_custom_lifecycle(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "done"]\n')
    t = tasks.create_task(env, "Do it")
    tasks.set_status(t, "analyzing", env)
    assert t.status == "analyzing"
    try:
        tasks.set_status(t, "review", env)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- removed-status tasks stay readable ---------------------------------- #

def test_task_with_removed_status_still_loads_and_renders_in_unknown_bucket(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "done"]\n')
    t = tasks.create_task(env, "Legacy task")
    t.status = "analyzing"
    tasks._save(t)
    # config edited afterwards, dropping "analyzing"
    _write_toml(env, '[board]\nstatuses = ["new", "done"]\n')
    reloaded = tasks.find(env, t.id)
    assert reloaded is not None
    assert reloaded.status == "analyzing"
    rendered = tasks.board(env)
    assert t.id in rendered
    assert "analyzing" in rendered


def test_by_status_includes_unknown_bucket_for_removed_status(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "done"]\n')
    t = tasks.create_task(env, "Legacy task")
    t.status = "analyzing"
    tasks._save(t)
    _write_toml(env, '[board]\nstatuses = ["new", "done"]\n')
    groups = tasks.by_status(env)
    assert "analyzing" in groups
    assert groups["analyzing"][0].id == t.id


# --- first/last semantics ------------------------------------------------ #

def test_default_lifecycle_first_is_todo_last_is_done():
    assert tasks.LIFECYCLE[0] == tasks.TODO
    assert tasks.LIFECYCLE[-1] == tasks.DONE


def test_custom_lifecycle_terminal_status_is_last_entry(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "shipped"]\n')
    pipeline = tasks.lifecycle(env)
    assert pipeline[0] == "new"
    assert pipeline[-1] == "shipped"


# --- external frontmatter field ------------------------------------------ #

def test_external_field_defaults_to_none(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    assert t.external is None
    assert tasks.to_dict(t)["external"] is None


def test_external_field_round_trips(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do it")
    t.external = {"provider": "linear", "id": "ENG-42", "url": "https://linear.app/x/ENG-42"}
    tasks._save(t)
    reloaded = tasks.find(env, t.id)
    assert reloaded.external == {"provider": "linear", "id": "ENG-42",
                                 "url": "https://linear.app/x/ENG-42"}


# --- Studio: no-config compatibility + payload-driven columns ------------ #

def test_no_config_projects_full_existing_suite_stays_green(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    payload = studio.board_payload(env)
    assert [s["name"] for s in payload["statuses"]] == tasks.LIFECYCLE
    row = payload["tasks"][0]
    assert row["id"] == t.id and row["status"] == "todo"


def test_board_payload_statuses_reflect_custom_config(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "done"]\n')
    payload = studio.board_payload(env)
    assert [s["name"] for s in payload["statuses"]] == ["new", "analyzing", "done"]


def test_set_task_status_payload_accepts_custom_status(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "done"]\n')
    t = tasks.create_task(env, "Do it")
    t.status = "new"
    tasks._save(t)
    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "analyzing"})
    assert result["ok"] is True
    assert tasks.find(env, t.id).status == "analyzing"


def test_set_task_status_payload_rejects_status_outside_custom_pipeline(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["new", "analyzing", "done"]\n')
    t = tasks.create_task(env, "Do it")
    t.status = "new"
    tasks._save(t)
    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "review"})
    assert result["ok"] is False


def test_studio_html_renders_board_order_as_javascript_var():
    # Regression: columns must come from the payload, not a hardcoded array —
    # applyBoardStatuses() reassigns BOARD_ORDER/BOARD_LABEL from BOARD.statuses.
    assert "let BOARD_ORDER" in studio._HTML
    assert "function applyBoardStatuses" in studio._HTML
    assert "applyBoardStatuses(BOARD.statuses)" in studio._HTML


# --- renaming a column ------------------------------------------------ #

def test_rename_moves_the_column_and_every_task_on_it(tmp_path):
    """A status name IS the value stored on each task, so renaming the column
    without migrating the tasks would strand them on a column that no longer
    exists."""
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["todo", "analyzing", "done"]\n')
    staying = tasks.create_task(env, "Untouched")
    moving = tasks.create_task(env, "On the renamed column")
    moving.status = "analyzing"
    tasks._save(moving)

    r = studio.rename_board_status_payload(env, {"from": "analyzing", "to": "in_analysis"})

    assert r["ok"] is True
    assert r["moved_tasks"] == [moving.id]
    assert tasks.lifecycle(env) == ["todo", "in_analysis", "done"]
    assert tasks.find(env, moving.id).status == "in_analysis"
    assert tasks.find(env, staying.id).status == "todo"


def test_rename_refuses_a_builtin_status(tmp_path):
    """harn's engine keys off the built-in names (which tasks an agent may
    pick up, in what order, what counts as done) — renaming one would quietly
    detach those tasks from the loop rather than relabel them."""
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["todo", "in_progress", "done"]\n')
    t = tasks.create_task(env, "Keeps its status")

    r = studio.rename_board_status_payload(env, {"from": "todo", "to": "backlog"})

    assert r["ok"] is False and "built-in" in r["error"]
    assert tasks.lifecycle(env) == ["todo", "in_progress", "done"]
    assert tasks.find(env, t.id).status == "todo"


def test_rename_refuses_an_unknown_or_colliding_name(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["todo", "analyzing", "done"]\n')
    assert studio.rename_board_status_payload(
        env, {"from": "nope", "to": "x"})["ok"] is False
    assert studio.rename_board_status_payload(
        env, {"from": "analyzing", "to": "done"})["ok"] is False
    assert tasks.lifecycle(env) == ["todo", "analyzing", "done"]


def test_rename_normalizes_the_new_name(tmp_path):
    env = _env(tmp_path)
    _write_toml(env, '[board]\nstatuses = ["todo", "analyzing", "done"]\n')
    r = studio.rename_board_status_payload(env, {"from": "analyzing", "to": "In Analysis"})
    assert r["ok"] is True
    assert tasks.lifecycle(env) == ["todo", "in_analysis", "done"]


def test_rename_leaves_hand_authored_status_tables_alone(tmp_path):
    """The `[[board.status]]` table form carries per-status external labels and
    takes priority over `statuses = [...]`; writing the flat form under it
    would appear to work while the app kept using the old table."""
    env = _env(tmp_path)
    _write_toml(env, (
        '[board]\n'
        '[[board.status]]\nname = "todo"\n'
        '[[board.status]]\nname = "analyzing"\n'
    ))
    r = studio.rename_board_status_payload(env, {"from": "analyzing", "to": "x"})
    assert r["ok"] is False and "harn.toml" in r["error"]
