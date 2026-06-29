"""The loop's review gate: end-to-end with a fake agent, plus reply parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

from harn import loop, tasks, state, scaffold, progress, ENV_DIRNAME
from harn.adapters.base import AgentResult


class FakeAdapter:
    name = "fake"

    def __init__(self, text="did the work"):
        self.text = text
        self.calls = 0

    def available(self):
        return True

    def run_turn(self, prompt, cwd):
        self.calls += 1
        return AgentResult(ok=True, text=self.text)


def _env_with_task(tmp_path: Path, *, wait=False) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    from tests.conftest import make_task
    make_task(env, "PRJ-001", title="Feat", priority=1,
              description="## What\nBuild a thing.\n\n## Done when\n- works")
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        f"[loop]\nmax_iterations = 6\nverify = false\nplanning = false\noracle = false\n"
        f"[notify]\nwait_for_reply = {str(wait).lower()}\n"
    )
    return env


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("approve", ("accept", "")),
        ("approve ship it with notes", ("accept", "ship it with notes")),
        ("ok", ("accept", "")),
        ("принято", ("accept", "")),
        ("одобряю, но добавь логи", ("accept", "но добавь логи")),
        ("use postgres instead", ("changes", "use postgres instead")),
        ("", ("changes", "")),
    ],
)
def test_parse_review_reply(reply, expected):
    assert loop._parse_review_reply(reply) == expected


def test_loop_submits_for_review_then_waits_cli(tmp_path, monkeypatch):
    env = _env_with_task(tmp_path, wait=False)  # no Telegram → CLI review path
    fake = FakeAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    phase = loop.run(tmp_path, env)

    assert phase == state.REVIEW
    t = tasks.find(env, "PRJ-001")
    assert t.status == tasks.REVIEW
    assert fake.calls == 2  # work turn + reconcile turn
    # progress log recorded the journey
    log = progress.tail(env)
    assert "started" in log and "submitted for review" in log


def test_cli_review_changes_then_accept(tmp_path, monkeypatch):
    env = _env_with_task(tmp_path, wait=False)
    fake = FakeAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)  # → review

    # user requests changes
    loop.review(env, "PRJ-001", approve=False, changes="rename the module")
    t = tasks.find(env, "PRJ-001")
    assert t.status == tasks.CHANGES_REQUESTED
    log_events = [e.event for e in t.review_log]
    assert "changes_requested" in log_events
    comment = next(e.comment for e in t.review_log if e.event == "changes_requested")
    assert "rename the module" in comment

    # agent reworks → back to review
    loop.run(tmp_path, env)
    assert tasks.find(env, "PRJ-001").status == tasks.REVIEW
    assert fake.calls == 4  # work×2 + reconcile×2

    # user accepts with notes
    loop.review(env, "PRJ-001", approve=True, notes="watch the edge case in parse()")
    done = tasks.find(env, "PRJ-001")
    assert done.status == tasks.DONE
    notes_entry = next((e for e in done.review_log if e.event == "accepted"), None)
    assert notes_entry and "watch the edge case" in notes_entry.notes

    # nothing left → DONE
    assert loop.run(tmp_path, env) == state.DONE


def test_loop_review_via_telegram(tmp_path, monkeypatch):
    env = _env_with_task(tmp_path, wait=True)
    fake = FakeAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    replies = iter(["use sqlite please", "approve great job"])

    class FakeHIL:
        def await_answer(self, text, *, state_dir, timeout_s, remind_every_s,
                         pre_grace_s, local_check):
            return (next(replies), "telegram")

        def wait_for_reply(self, text, *, state_dir, timeout_s, remind_every_s):
            return next(replies)

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda: FakeHIL()))

    phase = loop.run(tmp_path, env)
    assert phase == state.DONE
    done = tasks.find(env, "PRJ-001")
    assert done.status == tasks.DONE
    notes_entry = next((e for e in done.review_log if e.event == "accepted"), None)
    assert notes_entry and "great job" in notes_entry.notes
    assert fake.calls == 4  # work×2 + reconcile×2
