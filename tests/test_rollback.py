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


def test_task_patch_rollback_preserves_unrelated_later_wip(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = make_task(env, "PRJ-001")
    (root / "preexisting.txt").write_text("WIP before task\n")
    before = gitutil.checkpoint(root, t.id, "step-a")
    (root / "app.py").write_text("task change\n")
    patch = gitutil.patch_since(before, root, exclude=("harn_env/",))
    gitutil.save_patch_ref(root, t.id, "turn-1", patch)
    t.task_patch_refs = ["turn-1"]
    tasks._save(t)
    (root / "personal.txt").write_text("unrelated later WIP\n")

    result = loop.rollback(root, env, t.id, apply=True, reopen=True)

    assert result.ok
    assert (root / "app.py").read_text() == "def f():\n    return 1\n"
    assert (root / "personal.txt").read_text() == "unrelated later WIP\n"
    assert (root / "preexisting.txt").read_text() == "WIP before task\n"
    assert tasks.find(env, t.id).task_patch_refs == []


def test_rollback_tolerates_a_task_patch_whose_added_file_is_already_gone(tmp_path):
    """Reproduces a real Studio Restart failure: a task-owned patch records a
    step having ADDED a new file; something else (a later cleanup, a stray
    `git clean`, manual deletion) removes that file from the tree before the
    user restarts the task. Reverse-applying the patch then has nothing to
    delete, and `git apply --reverse` (non-3way) hard-fails on a nonexistent
    target -- but the tree is already in the patch's PRE-state, so there is
    nothing left to undo. Restart must not abort with "task patch conflicts
    with newer changes" in this case -- it should treat the reversal as
    already-satisfied and proceed to reopen the task."""
    root = _repo(tmp_path)
    env = _env(root)
    t = make_task(env, "PRJ-001")
    before = gitutil.checkpoint(root, t.id, "step-a")
    (root / "weather_result.json").write_text('{"temp": 22}\n')
    patch = gitutil.patch_since(before, root, exclude=("harn_env/",))
    gitutil.save_patch_ref(root, t.id, "turn-1", patch)
    t.task_patch_refs = ["turn-1"]
    tasks._save(t)
    (root / "weather_result.json").unlink()  # deleted out from under the task

    result = loop.rollback(root, env, t.id, apply=True, reopen=True)

    assert result.ok, result.message
    assert not (root / "weather_result.json").exists()
    fresh = tasks.find(env, t.id)
    assert fresh.task_patch_refs == []
    assert fresh.status == tasks.TODO


def test_rollback_still_rejects_a_genuine_conflict(tmp_path):
    """The tolerance added for an already-deleted new file (previous test)
    must not swallow a REAL conflict: if someone edited the exact lines the
    task's patch touched, reversing is genuinely impossible and rollback must
    still fail with the original message, untouched, and touch nothing."""
    root = _repo(tmp_path)
    env = _env(root)
    t = make_task(env, "PRJ-001")
    before = gitutil.checkpoint(root, t.id, "step-a")
    (root / "app.py").write_text("def f():\n    return 999  # task change\n")
    patch = gitutil.patch_since(before, root, exclude=("harn_env/",))
    gitutil.save_patch_ref(root, t.id, "turn-1", patch)
    t.task_patch_refs = ["turn-1"]
    tasks._save(t)
    # Someone else edits the SAME line afterwards -- a genuine conflict.
    (root / "app.py").write_text("def f():\n    return 42  # someone else's edit\n")

    result = loop.rollback(root, env, t.id, apply=True, reopen=True)

    assert not result.ok
    assert "conflicts with newer changes" in result.message
    assert "someone else's edit" in (root / "app.py").read_text()
    fresh = tasks.find(env, t.id)
    assert fresh.task_patch_refs == ["turn-1"]


def test_completed_turn_appends_task_owned_patch_ref(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = make_task(env, "PRJ-001")
    ref = gitutil.checkpoint(root, t.id, "step-a")
    t.stage_checkpoints["step-a"] = ref
    tasks._save(t)
    (root / "app.py").write_text("owned by task\n")

    loop._record_task_turn_patch(root, env, t.id, "step-a")

    fresh = tasks.find(env, t.id)
    assert len(fresh.task_patch_refs) == 1
    assert gitutil.load_patch_ref(root, t.id, fresh.task_patch_refs[0])


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


# --------------------------------------------------------------------------- #
# Per-stage checkpoints (git stash create + hidden ref) — the mechanism that
# guarantees "rerun this stage" undoes EXACTLY that stage's changes.
# --------------------------------------------------------------------------- #
def test_checkpoint_leaves_no_visible_trace(tmp_path):
    root = _repo(tmp_path)
    (root / "app.py").write_text("dirty change\n")   # uncommitted
    ref = gitutil.checkpoint(root, "PRJ-001", "execute")
    assert ref
    # no new commit, no visible stash entry
    log = subprocess.run(["git", "log", "--oneline"], cwd=root,
                         capture_output=True, text=True).stdout
    assert log.count("\n") == 1   # only the original "baseline" commit
    stash = subprocess.run(["git", "stash", "list"], cwd=root,
                           capture_output=True, text=True).stdout
    assert stash.strip() == ""
    # but a hidden ref pins it against GC
    refs = subprocess.run(["git", "for-each-ref", "refs/harn"], cwd=root,
                          capture_output=True, text=True).stdout
    assert "PRJ-001/execute" in refs
    # the working tree itself is untouched by taking the snapshot
    assert (root / "app.py").read_text() == "dirty change\n"


def test_checkpoint_restores_exactly_that_stage(tmp_path):
    root = _repo(tmp_path)
    (root / "app.py").write_text("before stage\n")
    ref = gitutil.checkpoint(root, "PRJ-001", "execute")

    # the stage runs and makes a mess
    (root / "app.py").write_text("stage broke this\n")
    (root / "new_file.py").write_text("junk\n")

    res = gitutil.rollback_to(ref, root, apply=True)
    assert res.ok
    assert (root / "app.py").read_text() == "before stage\n"
    assert not (root / "new_file.py").exists()


def test_checkpoint_on_clean_tree_uses_head(tmp_path):
    root = _repo(tmp_path)   # freshly committed, nothing dirty
    ref = gitutil.checkpoint(root, "PRJ-001", "plan")
    assert ref == gitutil.head(root)


def test_checkpoint_not_a_repo_returns_empty(tmp_path):
    assert gitutil.checkpoint(tmp_path, "PRJ-001", "execute") == ""


def test_reopen_clears_stage_checkpoints(tmp_path):
    root = _repo(tmp_path)
    env = _env(root)
    t = make_task(env, "PRJ-001", status=tasks.REVIEW)
    tasks.set_baseline(t, gitutil.head(root))
    ref = gitutil.checkpoint(root, "PRJ-001", "execute")
    t.stage_checkpoints["execute"] = ref
    tasks._save(t)

    loop.rollback(root, env, "PRJ-001", apply=True, reopen=True)

    fresh = tasks.find(env, "PRJ-001")
    assert fresh.stage_checkpoints == {}
    refs = subprocess.run(["git", "for-each-ref", "refs/harn/checkpoints/PRJ-001"],
                          cwd=root, capture_output=True, text=True).stdout
    assert refs.strip() == ""


def test_baseline_round_trips(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.set_baseline(t, "abc123")
    assert tasks.find(env, "PRJ-001").baseline_ref == "abc123"
    # set_baseline doesn't overwrite an existing baseline
    tasks.set_baseline(t, "different")
    assert tasks.find(env, "PRJ-001").baseline_ref == "abc123"
