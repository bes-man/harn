"""loop._run_turn's on_event captures message/tool_result content into the
task's own <id>.md Context section — the "real capture" half of the task
context markdown design (spec B). Today no shipping adapter streams
per-record events (that's the separate, in-progress adapter streaming work),
so this exercises the wiring itself with a stub adapter that DOES call
on_event, proving the choke point loop._run_turn provides is correctly wired
for whenever a streaming adapter lands."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import loop, tasks, scaffold, workflows, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "app.py").write_text("original\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "baseline"], tmp_path)
    return tmp_path


class StreamingAdapter:
    """Stands in for a future streaming-capable adapter: accepts `on_event`
    and calls it with a realistic mini event sequence before returning."""
    name = "fake"

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, on_event=None, **kw):
        if on_event:
            on_event({"kind": "tool", "phase": "started",
                      "title": "read_skill", "text": ""})
            on_event({"kind": "tool_result", "phase": "completed",
                      "title": "read_skill", "text": "the skill body content"})
            on_event({"kind": "status", "phase": "updated",
                      "title": "fake", "text": "should not be captured"})
        return AgentResult(ok=True, text="final turn summary")


def _env(tmp_path: Path) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    make_task(env, "PRJ-001", title="Feat", priority=1)
    workflows.save_task_plan(env, "PRJ-001", {"preamble": "", "nodes": [
        {"kind": "step", "id": "step-000001", "title": "Implement",
         "body": "do it", "required": [], "tools": [], "enabled": True}]})
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n')
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_tool_result_and_message_text_land_in_task_context(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    _wire(monkeypatch, StreamingAdapter())

    r = loop.run_step(root, env, "PRJ-001", "step-000001")

    assert r["ok"] is True
    t = tasks.find(env, "PRJ-001")
    assert "the skill body content" in t.context
    assert "step-000001" in t.context


def test_status_events_are_not_captured_into_context(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    _wire(monkeypatch, StreamingAdapter())

    loop.run_step(root, env, "PRJ-001", "step-000001")

    t = tasks.find(env, "PRJ-001")
    assert "should not be captured" not in t.context


def test_final_result_text_is_also_captured_as_a_message(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    _wire(monkeypatch, StreamingAdapter())

    loop.run_step(root, env, "PRJ-001", "step-000001")

    t = tasks.find(env, "PRJ-001")
    assert "final turn summary" in t.context
