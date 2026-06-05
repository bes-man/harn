"""Per-task rollback: capture a git baseline, restore the working tree to it."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import loop, tasks, gitutil
from .conftest import make_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "app.py").write_text("def f():\n    return 1\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "baseline"], tmp_path)
    return tmp_path


def _env(tmp_path: Path) -> Path:
    env = tmp_path / "harn_env"
    env.mkdir()
    return env


def test_gitutil_head_and_dirty(tmp_path):
    _repo(tmp_path)
    assert gitutil.is_repo(tmp_path)
    assert gitutil.head(tmp_path)               # has a commit hash
    assert not gitutil.is_dirty(tmp_path)
    (tmp_path / "app.py").write_text("changed\n")
    assert gitutil.is_dirty(tmp_path)


def test_rollback_restores_working_tree(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    baseline = gitutil.head(root)
    t = make_task(env, "PRJ-001")
    tasks.set_baseline(t, baseline)

    # Agent "changes" the file
    (root / "app.py").write_text("def f():\n    return 999  # bad\n")
    (root / "new.py").write_text("junk\n")

    # Dry run: reports files, doesn't touch them
    res = loop.rollback(root, env, "PRJ-001", apply=False)
    assert res.ok and "app.py" in res.files
    assert "999" in (root / "app.py").read_text()      # unchanged by dry run

    # Apply: restores
    res = loop.rollback(root, env, "PRJ-001", apply=True)
    assert res.ok
    assert (root / "app.py").read_text() == "def f():\n    return 1\n"


def test_rollback_reopen_resets_task(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = make_task(env, "PRJ-001", status=tasks.REVIEW)
    tasks.set_baseline(t, gitutil.head(root))
    tasks.record_decision(t, "use X", "because")
    tasks.set_scratchpad(t, "some note")

    (root / "app.py").write_text("changed\n")
    loop.rollback(root, env, "PRJ-001", apply=True, reopen=True)

    fresh = tasks.find(env, "PRJ-001")
    assert fresh.status == tasks.TODO
    assert fresh.scratchpad == ""
    assert fresh.decisions == []
    assert fresh.baseline_ref == ""
    assert any(e.event == "rolled_back" for e in fresh.review_log)


def test_rollback_no_baseline(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    make_task(env, "PRJ-001")            # no baseline captured
    res = loop.rollback(root, env, "PRJ-001", apply=True)
    assert not res.ok and "baseline" in res.message


def test_rollback_unknown_task(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    res = loop.rollback(root, env, "NOPE", apply=True)
    assert not res.ok


def test_rollback_never_touches_harn_env(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = make_task(env, "PRJ-001")
    tasks.set_baseline(t, gitutil.head(root))
    # commit the task file so it's part of git history after the baseline
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "add task"], root)

    (root / "app.py").write_text("changed\n")
    # mutate harn_env after baseline too
    tasks.set_scratchpad(t, "important working note")

    res = loop.rollback(root, env, "PRJ-001", apply=True)
    assert res.ok
    assert (root / "app.py").read_text() == "def f():\n    return 1\n"  # code restored
    # harn_env bookkeeping must be preserved, not rolled back
    assert tasks.find(env, "PRJ-001").scratchpad == "important working note"
    assert not any(f.startswith("harn_env/") for f in res.files)


def test_baseline_round_trips(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.set_baseline(t, "abc123")
    assert tasks.find(env, "PRJ-001").baseline_ref == "abc123"
    # set_baseline doesn't overwrite an existing baseline
    tasks.set_baseline(t, "different")
    assert tasks.find(env, "PRJ-001").baseline_ref == "abc123"
