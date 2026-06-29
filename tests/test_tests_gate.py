"""The test-writing gate: code-without-tests detection + one-nudge loop wiring."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import loop, scaffold, state, tasks, ENV_DIRNAME
from harn.adapters.base import AgentResult
from tests.conftest import make_task


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_missing_tests_true_when_only_code_changed(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "app.py").write_text("x = 2\n")
    assert loop._missing_tests(repo)


def test_missing_tests_false_when_tests_changed_too(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_x(): pass\n")
    _git(repo, "add", "-A")   # untracked files don't show in diff; stage them
    assert not loop._missing_tests(repo)


def test_missing_tests_false_for_non_code_changes(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "README.md").write_text("docs\n")
    _git(repo, "add", "-A")
    assert not loop._missing_tests(repo)


def test_missing_tests_false_outside_git(tmp_path):
    assert not loop._missing_tests(tmp_path)


def test_test_file_patterns():
    hit = loop._TEST_FILE_RE.search
    assert hit("tests/test_app.py")
    assert hit("src/__tests__/app.tsx")
    assert hit("pkg/foo_test.go")
    assert hit("src/app.spec.ts")
    assert hit("src/app.test.js")
    assert not hit("src/app.py")
    assert not hit("src/latest.py")        # 'test' substring alone ≠ a test file
    assert not hit("contest/entry.py")


class NudgeCountingAdapter:
    """Fake agent that changes code (never tests) every turn."""
    name = "fake"

    def __init__(self, repo: Path):
        self.repo = repo
        self.feedbacks: list[bool] = []   # did the prompt carry the nudge?
        self.calls = 0

    def available(self):
        return True

    def run_turn(self, prompt, cwd):
        self.calls += 1
        self.feedbacks.append("NO tests" in prompt)
        (self.repo / "app.py").write_text(f"x = {self.calls + 10}\n")
        return AgentResult(ok=True, text="changed code")


def test_loop_nudges_once_per_task(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    scaffold.setup(repo)
    env = repo / ENV_DIRNAME
    make_task(env, "PRJ-001", title="Feat", priority=1)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n'
        '[feedback]\ntest_cmd = ""\nrequire_tests = true\n'
        "[loop]\nmax_iterations = 6\nverify = false\nplanning = false\n"
        "oracle = false\n"
        "[notify]\nwait_for_reply = false\n"
    )
    fake = NudgeCountingAdapter(repo)
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    phase = loop.run(repo, env)

    # turn 1: code w/o tests → nudge; turn 2 carries the nudge, gate doesn't
    # re-fire (once per task) → task proceeds to review; turn 3 is reconcile.
    assert phase == state.REVIEW
    assert fake.calls == 3  # work×2 + reconcile×1
    assert fake.feedbacks == [False, True, False]  # reconcile prompt has no nudge
    assert tasks.find(env, "PRJ-001").status == tasks.REVIEW


def test_gate_off_by_config(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    scaffold.setup(repo)
    env = repo / ENV_DIRNAME
    make_task(env, "PRJ-001", title="Feat", priority=1)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n'
        '[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 6\nverify = false\nplanning = false\n"
        "oracle = false\n"
        "[notify]\nwait_for_reply = false\n"
    )
    fake = NudgeCountingAdapter(repo)
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    assert loop.run(repo, env) == state.REVIEW
    assert fake.calls == 2  # work turn + reconcile turn
