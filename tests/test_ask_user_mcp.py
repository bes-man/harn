"""MCP ask_user tool: posts the question as a durable task comment, so it
survives even if a later step's own block overwrites BLOCKED.md/state.question
(observed live — see docs/superpowers/specs 2026-07-20 spec-writer fixes)."""
from __future__ import annotations

from pathlib import Path

from harn import loop, state, tasks, ENV_DIRNAME


def _make_env(tmp_path: Path) -> Path:
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    (env / "skills").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    return env


def _tool_fn(env: Path, name: str, monkeypatch):
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    from harn import mcp_server as ms
    server = ms.build_server()
    return next(t.fn for t in server._tool_manager._tools.values() if t.name == name)


def test_ask_user_posts_a_comment_on_the_current_task(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    t = tasks.create_task(env, "Add payout support")
    st = state.State.load(env / "state")
    st.current_task = t.id
    st.save(env / "state")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "ask_user", monkeypatch)
    fn("Which option — A, B, or C?")

    fresh = tasks.find(env, t.id)
    assert len(fresh.comments) == 1
    assert fresh.comments[0].kind == "question"
    assert fresh.comments[0].author == "agent"
    assert fresh.comments[0].text == "Which option — A, B, or C?"


def test_ask_user_survives_no_current_task(tmp_path, monkeypatch):
    """No task in progress (current_task unset) — ask_user must still block,
    just without a comment to attribute anywhere."""
    env = _make_env(tmp_path)
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "ask_user", monkeypatch)
    result = fn("Which option?")

    assert "recorded" in result.lower()
    assert state.read_block_question(env / "state") == "Which option?"


def test_run_step_sets_current_task(tmp_path, monkeypatch):
    """run_step() is the role-dispatch path (roles_runner._run_steps calls it
    directly, never through run()'s own current_task assignment) — without
    setting it here, state.current_task stays None for the whole run."""
    import subprocess
    from harn import scaffold, workflows
    from harn.adapters.base import AgentResult

    def _git(args, cwd):
        subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)

    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 40\noracle = false\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "base"], tmp_path)

    t = tasks.create_task(env, "T")
    plan = {"preamble": "", "nodes": [
        {"kind": "step", "title": "Step 1", "body": "do it", "id": "step-000001",
         "agent": "", "model": "", "effort": "", "temperature": "",
         "required": [], "tools": [], "enabled": True}]}
    workflows.save_task_plan(env, t.id, plan)

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                    temperature=None):
            return AgentResult(ok=True, text="done")

    monkeypatch.setattr(loop, "get_adapter", lambda n: FakeAdapter())
    loop.run_step(tmp_path, env, t.id, "step-000001")

    assert state.State.load(env / "state").current_task == t.id
