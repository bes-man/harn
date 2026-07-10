"""cmd_trace rendering: every allow-listed field must actually show up in the
printed line, not just be present on the event dict. Regression coverage for
the Phase 4 gap where tool_used's `tool`/`step_id` fields were dropped."""
from __future__ import annotations

from types import SimpleNamespace

from harn import cli, events, ENV_DIRNAME


def _args(path, task_id=None, run_id=None):
    return SimpleNamespace(path=str(path), task_id=task_id, run_id=run_id)


def test_trace_renders_tool_used_tool_and_step_id(tmp_path, capsys):
    env = tmp_path / ENV_DIRNAME
    events.new_run(env, kind="loop")
    events.emit(env, "tool_used", task_id="T1", tool="run_tests", step_id="s2")

    rc = cli.cmd_trace(_args(tmp_path))

    assert rc == 0
    out = capsys.readouterr().out
    assert "tool=run_tests" in out
    assert "step_id=s2" in out


def test_trace_renders_context_read_step_id(tmp_path, capsys):
    env = tmp_path / ENV_DIRNAME
    events.new_run(env, kind="loop")
    events.emit(env, "context_read", task_id="T1", kind="skill",
                name="standards", step_id="s1")

    rc = cli.cmd_trace(_args(tmp_path))

    assert rc == 0
    out = capsys.readouterr().out
    assert "kind=skill" in out
    assert "name=standards" in out
    assert "step_id=s1" in out
