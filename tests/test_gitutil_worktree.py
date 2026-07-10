"""Worktree + patch plumbing for parallel workflow steps (Phase 3): isolate
each parallel step in its own worktree off a shared checkpoint, capture its
diff as a patch, and apply/reverse-apply that patch against the main tree."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import gitutil


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


def test_create_and_remove_worktree(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    assert gitutil.create_worktree(root, base_ref, wt) is True
    assert (wt / "app.py").read_text() == "original\n"
    gitutil.remove_worktree(root, wt)
    assert not wt.exists()


def test_diff_as_patch_captures_worktree_changes(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("changed by step A\n")
    (wt / "new_file.py").write_text("brand new\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    assert "changed by step A" in patch
    assert "new_file.py" in patch
    gitutil.remove_worktree(root, wt)


def test_diff_as_patch_empty_when_no_changes(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    assert gitutil.diff_as_patch(wt, base_ref) == ""
    gitutil.remove_worktree(root, wt)


def test_apply_patch_applies_cleanly_to_main_tree(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("changed by step A\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    gitutil.remove_worktree(root, wt)

    assert gitutil.apply_patch(root, patch) is True
    assert (root / "app.py").read_text() == "changed by step A\n"


def test_apply_patch_reverse_undoes_it(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("changed by step A\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    gitutil.remove_worktree(root, wt)
    gitutil.apply_patch(root, patch)

    assert gitutil.apply_patch(root, patch, reverse=True) is True
    assert (root / "app.py").read_text() == "original\n"


def test_apply_patch_conflict_reports_false_not_raise(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("step A's version\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    gitutil.remove_worktree(root, wt)
    # main tree ALREADY has a conflicting change on the same line
    (root / "app.py").write_text("someone else's incompatible edit\n")

    assert gitutil.apply_patch(root, patch) is False
    # main tree is untouched by the failed attempt
    assert (root / "app.py").read_text() == "someone else's incompatible edit\n"


def test_save_and_load_patch_ref(tmp_path):
    root = _repo(tmp_path)
    gitutil.save_patch_ref(root, "T1", "step-a", "diff --git a/x b/x\n+hello\n")
    assert gitutil.load_patch_ref(root, "T1", "step-a") == "diff --git a/x b/x\n+hello\n"


def test_load_patch_ref_missing_returns_empty(tmp_path):
    root = _repo(tmp_path)
    assert gitutil.load_patch_ref(root, "T1", "nope") == ""
