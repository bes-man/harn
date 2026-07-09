"""Command steps (Type: command) + On-fail handler dispatch — Phase 2 of the
step engine. A command step runs a shell command instead of an agent turn
(harn/feedback.py's run_feedback); on failure with a resolvable On-fail
target, the engine dispatches that agent step, then retries the command."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import loop, tasks, workflows, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


class RecordingAdapter:
    name = "fake"
    def __init__(self, texts=None):
        self.calls = []
        self._texts = list(texts or ["fixed it"])
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        i = min(len(self.calls) - 1, len(self._texts) - 1)
        return AgentResult(ok=True, text=self._texts[i])


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _project(tmp_path, nodes):
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 20\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    workflows.save_task_plan(env, t.id, {"preamble": "", "nodes": nodes})
    return env, t


def _step(title, **kw):
    base = {"kind": "step", "title": title, "body": "", "id": "", "agent": "",
            "model": "", "effort": "", "temperature": "", "type": "",
            "command": "", "on_fail": "", "required": [], "tools": [],
            "enabled": True}
    base.update(kw)
    return base


def test_successful_command_step_advances_without_agent_call(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="true"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 0   # no agent turn at all
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "ok"


def test_failing_command_step_with_no_onfail_advances_and_records_failure(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="false"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "failed"
    assert fresh.status == tasks.REVIEW   # still submitted — Phase-1-equivalent, no special handling


def test_onfail_handler_dispatched_then_command_retried_until_it_passes(tmp_path, monkeypatch):
    """The classic case: tests fail -> Fix step runs -> tests re-run -> pass."""
    marker = tmp_path / "should_pass"
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command",
              command=f"test -f {marker}", on_fail="step-fix"),
        _step("Fix tests", id="step-fix"),
    ])
    class WritingAdapter(RecordingAdapter):
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                    temperature=None):
            self.calls.append({"prompt": prompt})
            marker.write_text("now it passes")
            return AgentResult(ok=True, text="fixed it")
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 1
    assert "Tests" in fake.calls[0]["prompt"] or "failed" in fake.calls[0]["prompt"].lower()
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "ok"
    assert fresh.step_results["step-fix"]["status"] == "ok"


def test_onfail_pointing_at_command_step_is_a_config_error_not_a_crash(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="false", on_fail="step-t2"),
        _step("Lint", id="step-t2", type="command", command="true"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)   # must not raise
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "failed"
    assert len(fake.calls) == 0


def test_persistent_failure_terminates_at_max_iterations_not_forever(tmp_path, monkeypatch):
    """The handler never actually fixes anything -> command keeps failing ->
    the SAME max_iterations budget that bounds ordinary retries also bounds
    this fail/handler/retry cycle. No new counter; this proves it."""
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="false", on_fail="step-fix"),
        _step("Fix tests", id="step-fix"),
    ])
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 6\n[notify]\nwait_for_reply = false\n")
    fake = RecordingAdapter(texts=["still broken"] * 10)   # handler never fixes it
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)   # must return, not hang
    assert len(fake.calls) >= 2   # handler was retried more than once before the budget ran out
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "failed"   # command never got to "ok"


def test_rework_clears_done_ids_so_command_step_rewalks(tmp_path, monkeypatch):
    """Regression: `done_ids` (auto_done[task.id]) is an in-memory registry
    that persists for the whole run() call. A command step force-advances
    into it on any terminal outcome. If rework resolves INLINE within the
    SAME run() call (the Telegram wait_for_reply auto-review path — see
    test_loop_review_via_telegram), the on-disk ledger gets cleared for
    rework but `done_ids` used to survive, silently skipping the command
    step's re-walk. Assert it re-executes instead."""
    from harn import state
    counter = tmp_path / "counter"
    # run_feedback uses shlex.split (no shell), so drive the append via a
    # tiny script instead of relying on shell redirection.
    script = tmp_path / "count.py"
    script.write_text(
        "import pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "p.write_text((p.read_text() if p.exists() else '') + 'ran\\n')\n"
    )
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command",
              command=f"python3 {script} {counter}"),
    ])
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 20\n[notify]\nwait_for_reply = true\n")
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    replies = iter(["needs work", "approve"])

    class FakeHIL:
        def wait_for_reply(self, text, *, state_dir, timeout_s, remind_every_s):
            return next(replies)

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda: FakeHIL()))

    phase = loop.run(tmp_path, env)
    assert phase == state.DONE
    fresh = tasks.find(env, t.id)
    assert fresh.status == tasks.DONE
    # The command step must have run TWICE — once before the rework, once
    # after — proving `done_ids` was cleared, not just the on-disk ledger.
    assert counter.read_text().count("ran") == 2
    assert fresh.step_results["step-t1"]["status"] == "ok"


def test_checkpoint_captured_for_command_step(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="true"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    fresh = tasks.find(env, t.id)
    assert "step-t1" in fresh.stage_checkpoints
