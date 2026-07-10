"""Studio board: task list + workflow assignment + launch/stop wiring
(harn/studio.py's board_payload/set_task_workflow/launch_task/stop_task)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from harn import studio, tasks, workflows, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


def _fake_popen(pid=4242):
    proc = MagicMock()
    proc.pid = pid
    return proc


def test_board_payload_lists_full_task_detail(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth", description="## What\ndo it")
    payload = studio.board_payload(env)
    assert payload["run"] is None
    row = payload["tasks"][0]
    assert row["id"] == t.id and row["status"] == "todo"
    assert "scratchpad" in row and "review_log" in row and "decisions" in row


def test_board_payload_includes_step_usage_when_present(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    t.step_results["s1"] = {"status": "ok", "usage": {
        "skills": {"standards": "used", "testing": "unused_recommended"},
        "tools": {"run_tests": "unused_required"}}}
    tasks._save(t)
    payload = studio.board_payload(env)
    row = next(r for r in payload["tasks"] if r["id"] == t.id)
    assert row["step_results"]["s1"]["usage"]["skills"]["standards"] == "used"
    assert row["step_results"]["s1"]["usage"]["skills"]["testing"] == "unused_recommended"
    assert row["step_results"]["s1"]["usage"]["tools"]["run_tests"] == "unused_required"


def test_set_task_workflow_assigns_known_preset(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    workflows.create(env, name="qa", title="QA")
    r = studio.set_task_workflow(env, {"task_id": t.id, "workflow": "qa"})
    assert r["ok"] is True and r["workflow"] == "qa"
    assert tasks.find(env, t.id).workflow == "qa"


def test_set_task_workflow_rejects_unknown_preset(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    r = studio.set_task_workflow(env, {"task_id": t.id, "workflow": "nonexistent"})
    assert r["ok"] is False
    assert tasks.find(env, t.id).workflow is None


def test_set_task_workflow_clears_with_empty_string(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth", workflow="default")
    r = studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})
    assert r["ok"] is True and r["workflow"] is None


def test_set_task_workflow_unknown_task(tmp_path):
    env = _env(tmp_path)
    r = studio.set_task_workflow(env, {"task_id": "NOPE", "workflow": ""})
    assert r["ok"] is False


def test_launch_task_starts_background_run(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()), \
         patch("harn.runner.os.kill"):
        r = studio.launch_task(env, {"task_id": t.id})
        assert r["ok"] is True and r["task_id"] == t.id
        board = studio.board_payload(env)
        assert board["run"]["task_id"] == t.id


def test_launch_task_unknown_task_id(tmp_path):
    env = _env(tmp_path)
    r = studio.launch_task(env, {"task_id": "NOPE"})
    assert r["ok"] is False


def test_stop_task_with_no_active_run(tmp_path):
    env = _env(tmp_path)
    r = studio.stop_task(env, {})
    assert r["ok"] is False


def test_launch_then_stop_round_trip(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    with patch("harn.runner.subprocess.Popen", return_value=_fake_popen()), \
         patch("harn.runner.os.kill"):
        studio.launch_task(env, {"task_id": t.id})
        r = studio.stop_task(env, {})
        assert r["ok"] is True
        assert studio.board_payload(env)["run"] is None
