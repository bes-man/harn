"""Studio board: task list + workflow assignment + launch/stop wiring
(harn/studio.py's board_payload/set_task_workflow/launch_task/stop_task)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from harn import runner, state, studio, tasks, workflows, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


def _fake_popen(pid=4242):
    proc = MagicMock()
    proc.pid = pid
    proc.wait.return_value = 0
    return proc


def test_board_payload_lists_full_task_detail(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth", description="## What\ndo it")
    payload = studio.board_payload(env)
    assert payload["run"] is None
    row = payload["tasks"][0]
    assert row["id"] == t.id and row["status"] == "todo"
    assert "scratchpad" in row and "review_log" in row and "decisions" in row


def test_board_payload_keeps_last_run_failure_visible_after_process_exits(tmp_path):
    env = _env(tmp_path)
    (env / "state").mkdir()
    runner._last_run_path(env).write_text(
        '{"task_id":"PRJ-001","exit_code":1,"reason":"Failed to authenticate"}',
        encoding="utf-8",
    )

    payload = studio.board_payload(env)

    assert payload["last_run"] == {
        "task_id": "PRJ-001", "exit_code": 1, "reason": "Failed to authenticate"
    }


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


def test_set_task_workflow_marks_confirmed(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    assert t.workflow_confirmed is False
    studio.set_task_workflow(env, {"task_id": t.id, "workflow": ""})
    reloaded = tasks.find(env, t.id)
    assert reloaded.workflow_confirmed is True


# --- create task + status-change routes (Phase 6 Task 3) ------------------ #

def test_create_task_payload_creates_a_todo_task(tmp_path):
    env = _env(tmp_path)
    result = studio.create_task_payload(env, {"title": "Do the thing"})
    assert result["ok"] is True
    t = tasks.find(env, result["task_id"])
    assert t.status == tasks.TODO
    assert t.title == "Do the thing"


def test_create_task_payload_rejects_empty_title(tmp_path):
    env = _env(tmp_path)
    result = studio.create_task_payload(env, {"title": "  "})
    assert result["ok"] is False


def test_create_task_payload_with_explicit_workflow_marks_confirmed(tmp_path):
    env = _env(tmp_path)
    workflows.create(env, name="qa", title="QA")
    result = studio.create_task_payload(env, {"title": "Do the thing", "workflow": "qa"})
    assert result["ok"] is True
    t = tasks.find(env, result["task_id"])
    assert t.workflow == "qa"
    assert t.workflow_confirmed is True


def test_create_task_payload_without_workflow_leaves_unconfirmed(tmp_path):
    env = _env(tmp_path)
    result = studio.create_task_payload(env, {"title": "Do the thing", "workflow": ""})
    assert result["ok"] is True
    t = tasks.find(env, result["task_id"])
    assert t.workflow is None
    assert t.workflow_confirmed is False


def test_create_task_payload_with_workflow_can_start_immediately(tmp_path, monkeypatch):
    env = _env(tmp_path)
    workflows.create(env, name="qa", title="QA")
    result = studio.create_task_payload(env, {"title": "Do the thing", "workflow": "qa"})
    task_id = result["task_id"]

    launched = {}
    def fake_launch(pr, ed, tid, *, auto=False):
        launched["task_id"] = tid
        return {"ok": True, "pid": 12345, "task_id": tid, "auto": auto}
    monkeypatch.setattr(studio.runner_mod, "launch", fake_launch)

    status_result = studio.set_task_status_payload(env, {"task_id": task_id, "status": "in_progress"})
    assert status_result["ok"] is True
    assert launched["task_id"] == task_id
    reloaded = tasks.find(env, task_id)
    assert reloaded.status == "in_progress"


def _mcp_tool_fn(env, name, monkeypatch):
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    from harn import mcp_server as ms
    server = ms.build_server()
    return next(t.fn for t in server._tool_manager._tools.values() if t.name == name)


def test_mcp_create_task_with_explicit_workflow_marks_confirmed(tmp_path, monkeypatch):
    env = _env(tmp_path)
    workflows.create(env, name="qa", title="QA")
    create_task = _mcp_tool_fn(env, "create_task", monkeypatch)

    create_task(title="Do the thing", description="## What\ndo it", prds=[],
                skills=[], task_id="T1", workflow="qa")

    t = tasks.find(env, "T1")
    assert t is not None
    assert t.workflow == "qa"
    assert t.workflow_confirmed is True


def test_mcp_create_task_without_workflow_leaves_unconfirmed(tmp_path, monkeypatch):
    env = _env(tmp_path)
    create_task = _mcp_tool_fn(env, "create_task", monkeypatch)

    create_task(title="Do the thing", description="## What\ndo it", prds=[],
                skills=[], task_id="T2")

    t = tasks.find(env, "T2")
    assert t is not None
    assert t.workflow is None
    assert t.workflow_confirmed is False


def test_status_payload_refuses_in_progress_before_flow_confirmed(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "in_progress"})
    assert result["ok"] is False
    assert "flow" in result["error"].lower()
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == tasks.TODO


def test_status_payload_launches_a_run_once_flow_confirmed(tmp_path, monkeypatch):
    env = _env(tmp_path)
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
    env = _env(tmp_path)
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
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")

    def fail_if_called(*a, **kw):
        raise AssertionError("launch should not be called for a non-in_progress transition")
    monkeypatch.setattr(studio.runner_mod, "launch", fail_if_called)

    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "review"})
    assert result["ok"] is True
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == "review"


def test_status_payload_refuses_when_run_active_for_task(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")

    monkeypatch.setattr(studio.runner_mod, "active",
                         lambda ed: {"task_id": t.id, "pid": 999})

    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "review"})
    assert result["ok"] is False
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == tasks.TODO


def test_status_payload_unknown_status(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Do the thing")
    result = studio.set_task_status_payload(env, {"task_id": t.id, "status": "bogus"})
    assert result["ok"] is False


def test_status_payload_unknown_task(tmp_path):
    env = _env(tmp_path)
    result = studio.set_task_status_payload(env, {"task_id": "NOPE", "status": "review"})
    assert result["ok"] is False


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


# --- blocked question view + answer (studio-side of ask_user/BLOCKED) ----- #

def test_blocked_question_payload_returns_question_when_blocked(tmp_path):
    env = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id)
    st.block("Should I use approach A or B? I recommend A because...")
    st.save(state_dir)
    payload = studio.blocked_question_payload(env, task.id)
    assert payload["question"] == "Should I use approach A or B? I recommend A because..."


def test_blocked_question_payload_returns_none_when_not_blocked(tmp_path):
    env = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    payload = studio.blocked_question_payload(env, task.id)
    assert payload["question"] is None


def test_answer_route_calls_loop_answer_and_clears_the_block(tmp_path):
    env = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id)
    st.block("Pick one.")
    st.save(state_dir)
    with patch("harn.studio.runner_mod.launch", return_value={"ok": True}):
        result = studio.answer_payload(env, task.id, "Go with A.")
    assert result.get("ok") is True
    reloaded = state.State.load(state_dir)
    assert reloaded.phase != state.BLOCKED
    assert reloaded.last_answer == "Go with A."


def test_answer_route_restarts_the_blocked_task(tmp_path):
    env = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id)
    st.block("Pick one.")
    st.save(state_dir)
    with patch("harn.studio.runner_mod.launch", return_value={"ok": True}) as launch:
        result = studio.answer_payload(env, "different-ui-task-id", "Go with A.")
    launch.assert_called_once_with(env.parent, env, task.id)
    assert result == {"ok": True, "task_id": task.id, "resumed": True}


def test_answer_route_keeps_answer_when_resume_is_refused(tmp_path):
    env = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id)
    st.block("Pick one.")
    st.save(state_dir)
    with patch(
        "harn.studio.runner_mod.launch",
        return_value={"ok": False, "error": "another run is already active"},
    ):
        result = studio.answer_payload(env, task.id, "Go with A.")
    assert result == {
        "ok": True,
        "task_id": task.id,
        "resumed": False,
        "warning": "another run is already active",
    }
    reloaded = state.State.load(state_dir)
    assert reloaded.last_answer == "Go with A."


def test_answer_route_records_answer_without_current_task(tmp_path):
    env = _env(tmp_path)
    state_dir = env / "state"
    st = state.State()
    st.block("Pick one.")
    st.save(state_dir)
    with patch("harn.studio.runner_mod.launch") as launch:
        result = studio.answer_payload(env, "ui-task-id", "Go with A.")
    launch.assert_not_called()
    assert result == {
        "ok": True,
        "resumed": False,
        "warning": "no current task to resume",
    }
    assert state.State.load(state_dir).last_answer == "Go with A."


def test_answer_payload_rejects_empty_text(tmp_path):
    env = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id)
    st.block("Pick one.")
    st.save(state_dir)
    result = studio.answer_payload(env, task.id, "   ")
    assert "error" in result
    # the block is untouched
    assert state.State.load(state_dir).phase == state.BLOCKED
