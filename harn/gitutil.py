"""Thin git helpers for per-task baselines and rollback.

harn never *requires* git — every function degrades gracefully (returns "" / a
disabled result) when the project isn't a git repo or git isn't installed.
"""
from __future__ import annotations

import subprocess
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _run(args: list[str], cwd: Path, timeout: int = 15) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, "", "git not available"


def is_repo(cwd: Path) -> bool:
    code, out, _ = _run(["rev-parse", "--is-inside-work-tree"], cwd)
    return code == 0 and out == "true"


def head(cwd: Path) -> str:
    """Current HEAD commit hash, or '' if not a repo / no commits."""
    code, out, _ = _run(["rev-parse", "HEAD"], cwd)
    return out if code == 0 else ""


def checkpoint(cwd: Path, task_id: str, stage: str) -> str:
    """Snapshot the CURRENT working tree (staged + unstaged + untracked) right
    before a stage's turn runs, so a later "rerun this stage" can restore
    EXACTLY this starting point via `rollback_to` — undoing only that attempt,
    not the task's whole history.

    Uses `git stash create`: a dangling commit object capturing the full
    working-tree state, WITHOUT touching the branch, HEAD, or the working tree
    itself, and WITHOUT appearing in `git stash list` or `git log` — a real git
    primitive, not custom plumbing. Pinned against garbage collection with a
    hidden ref (refs/harn/checkpoints/<task_id>/<stage>) that no normal git
    command surfaces.

    If the tree is completely clean, there's nothing to stash — HEAD itself
    already IS that state, so restoring later is correctly a no-op.

    Returns the checkpoint commit hash, or '' if this isn't a git repo / git
    isn't available (checkpointing is best-effort, like the rest of this
    module — never blocks a turn from running).
    """
    if not is_repo(cwd):
        return ""
    if not is_dirty(cwd):
        ref = head(cwd)
    else:
        fd, index_path = tempfile.mkstemp(prefix="harn-snapshot-index-")
        os.close(fd)
        try:
            os.unlink(index_path)
            env = {**os.environ, "GIT_INDEX_FILE": index_path}
            def run(args, *, input_text=None):
                return subprocess.run(["git", *args], cwd=cwd, env=env,
                                      input=input_text, capture_output=True,
                                      text=True, timeout=30)
            base = head(cwd)
            if not base or run(["read-tree", base]).returncode != 0 \
                    or run(["add", "-A"]).returncode != 0:
                return ""
            tree = run(["write-tree"]).stdout.strip()
            commit = run(["commit-tree", tree, "-p", base],
                         input_text=f"harn:{task_id}:{stage}\n")
            ref = commit.stdout.strip() if commit.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            ref = ""
        finally:
            try:
                os.unlink(index_path)
            except OSError:
                pass
    if not ref:
        return ""
    _run(["update-ref", f"refs/harn/checkpoints/{task_id}/{stage}", ref], cwd)
    return ref


def clear_checkpoints(cwd: Path, task_id: str) -> None:
    """Delete every hidden checkpoint ref for a task (a full rerun/reopen makes
    the old per-stage checkpoints stale — this stops them accumulating as
    dangling refs forever)."""
    if not is_repo(cwd):
        return
    code, out, _ = _run(["for-each-ref", "--format=%(refname)",
                        f"refs/harn/checkpoints/{task_id}"], cwd)
    if code != 0:
        return
    for ref in out.splitlines():
        if ref.strip():
            _run(["update-ref", "-d", ref.strip()], cwd)


def is_dirty(cwd: Path) -> bool:
    code, out, _ = _run(["status", "--porcelain"], cwd)
    return code == 0 and bool(out)


def changed_since(ref: str, cwd: Path, exclude: tuple[str, ...] = ()) -> list[str]:
    """Files changed (committed + working tree) since `ref`.

    `exclude` is a tuple of path prefixes to skip (e.g. ('harn_env/',)) so a
    rollback restores project code without clobbering harn's own bookkeeping.
    """
    if not ref:
        return []
    files: set[str] = set()
    for args in (["diff", "--name-only", ref], ["diff", "--name-only"]):
        code, out, _ = _run(args, cwd)
        if code == 0 and out:
            files.update(out.splitlines())
    # untracked
    code, out, _ = _run(["ls-files", "--others", "--exclude-standard"], cwd)
    if code == 0 and out:
        files.update(out.splitlines())
    return sorted(
        f for f in files if f and not any(f.startswith(p) for p in exclude)
    )


def diffstat_since(ref: str, cwd: Path) -> str:
    if not ref:
        return ""
    code, out, _ = _run(["diff", "--stat", ref], cwd)
    return out if code == 0 else ""


def files_touched_vs(ref: str, cwd: Path, exclude: tuple[str, ...] = ()) -> list[str]:
    """Files that differ between `ref` and the CURRENT working tree — i.e. ONLY
    what changed SINCE that checkpoint, precisely.

    Unlike `changed_since` (which also unions the raw index-vs-worktree diff and
    is meant for rollback), this runs a single `git diff --name-only <ref>`, so
    when `ref` is a pre-turn checkpoint that already captured the developer's
    unrelated WIP, the result is exactly what THIS turn added — the developer's
    pre-existing uncommitted edits do not leak in. `exclude` skips path prefixes
    (e.g. 'harn_env/'). Empty/invalid ref → [] (best-effort, never raises)."""
    if not ref:
        return []
    code, out, _ = _run(["diff", "--name-only", ref], cwd)
    if code != 0 or not out:
        return []
    return [f for f in out.splitlines()
            if f and not any(f.startswith(p) for p in exclude)]


@dataclass
class RollbackResult:
    ok: bool
    message: str
    files: list[str]


def rollback_to(ref: str, cwd: Path, *, apply: bool,
                exclude: tuple[str, ...] = ()) -> RollbackResult:
    """Restore the working tree to `ref`.

    With apply=False (default) it's a dry run: report what would change.
    With apply=True it runs `git checkout <ref> -- <files>` for files changed
    since the baseline (working-tree only — commits are left intact).
    `exclude` skips path prefixes (e.g. harn's own `harn_env/`).
    """
    if not is_repo(cwd):
        return RollbackResult(False, "not a git repository", [])
    if not ref:
        return RollbackResult(False, "no baseline recorded for this task", [])
    code, _, _ = _run(["cat-file", "-e", ref], cwd)
    if code != 0:
        return RollbackResult(False, f"baseline commit {ref[:8]} not found", [])

    files = changed_since(ref, cwd, exclude=exclude)
    if not files:
        return RollbackResult(True, "nothing changed since the baseline", [])
    if not apply:
        return RollbackResult(
            True, f"would restore {len(files)} file(s) to {ref[:8]} (dry run)", files
        )
    # Split: files that existed at the baseline (restore them) vs. files created
    # after it (delete them). Mixing the two breaks `git checkout`'s atomicity.
    existed, created = [], []
    for f in files:
        code, _, _ = _run(["cat-file", "-e", f"{ref}:{f}"], cwd)
        (existed if code == 0 else created).append(f)
    if existed:
        _run(["checkout", ref, "--", *existed], cwd)
    for f in created:
        try:
            (cwd / f).unlink()
        except OSError:
            pass
    return RollbackResult(True, f"restored {len(files)} file(s) to {ref[:8]}", files)


def create_worktree(cwd: Path, base_ref: str, worktree_path: Path) -> bool:
    """Create a detached worktree at `worktree_path`, checked out at
    `base_ref`. Best-effort like the rest of this module: returns whether it
    succeeded, never raises."""
    if not is_repo(cwd) or not base_ref:
        return False
    code, _, _ = _run(["worktree", "add", "--detach", str(worktree_path), base_ref], cwd)
    return code == 0


def remove_worktree(cwd: Path, worktree_path: Path) -> None:
    """Remove a worktree created by `create_worktree` (best-effort). Also
    handles the case the worktree directory was already deleted out from
    under git."""
    if not is_repo(cwd):
        return
    code, _, _ = _run(["worktree", "remove", "--force", str(worktree_path)], cwd)
    if code != 0:
        # Directory may already be gone; prune the stale admin entry too.
        _run(["worktree", "prune"], cwd)


def diff_as_patch(worktree_path: Path, base_ref: str,
                  exclude: tuple[str, ...] = ()) -> str:
    """Full unified diff of `worktree_path` against `base_ref`, covering
    staged, unstaged, and untracked-but-new files. Returns "" if there's no
    diff (or on any git failure)."""
    if not is_repo(worktree_path) or not base_ref:
        return ""
    pathspec = [".", *[f":(exclude){p}" for p in exclude]]
    _run(["add", "-A", "--", *pathspec], worktree_path)
    code, out, _ = _run(["diff", "--cached", base_ref, "--", *pathspec], worktree_path)
    return out + "\n" if code == 0 and out else ""


def patch_since(ref: str, cwd: Path, exclude: tuple[str, ...] = ()) -> str:
    """Return the exact working-tree patch since ``ref`` without touching index."""
    if not is_repo(cwd) or not ref:
        return ""
    fd, index_path = tempfile.mkstemp(prefix="harn-index-")
    os.close(fd)
    try:
        os.unlink(index_path)  # read-tree requires a missing or valid index
        env = {**os.environ, "GIT_INDEX_FILE": index_path}
        def run(args):
            return subprocess.run(["git", *args], cwd=cwd, env=env,
                                  capture_output=True, text=True, timeout=30)
        if run(["read-tree", ref]).returncode != 0:
            return ""
        pathspec = [".", *[f":(exclude){p}" for p in exclude]]
        if run(["add", "-A", "--", *pathspec]).returncode != 0:
            return ""
        result = run(["diff", "--cached", "--binary", ref, "--", *pathspec])
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def apply_patch(cwd: Path, patch_text: str, *, reverse: bool = False,
                three_way: bool = True) -> bool:
    """Apply `patch_text` to the working tree at `cwd` via `git apply --3way`
    (reverse-applies if `reverse=True`). Returns whether it applied cleanly;
    never raises."""
    if not is_repo(cwd) or not patch_text:
        return False
    args = ["apply"]
    if three_way:
        args.append("--3way")
    if reverse:
        args.append("--reverse")
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, input=patch_text, capture_output=True,
            text=True, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def patch_reverse_is_moot(cwd: Path, patch_text: str) -> bool:
    """True if reverse-applying `patch_text` is unnecessary because the tree
    is already in the patch's PRE-state -- i.e. forward-applying it (dry run)
    would succeed cleanly. Covers the case a task-owned patch recorded a step
    ADDING a file that something else (a later cleanup, a stray `git clean`,
    manual deletion) has since removed: there is nothing left for the reverse
    apply to delete, but the desired end state (file absent) already holds, so
    the reversal should count as satisfied rather than a hard conflict."""
    if not is_repo(cwd) or not patch_text:
        return False
    try:
        r = subprocess.run(
            ["git", "apply", "--check"], cwd=cwd, input=patch_text,
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def stage_all(cwd: Path) -> None:
    """`git add -A` at `cwd` — best-effort, never raises. Used after an
    agent-merge turn resolves `git apply --3way` conflict markers directly in
    the working tree, to clear any unmerged/conflicted index entries left
    behind by the failed apply (so later `checkpoint`/`diff_as_patch` calls on
    this tree don't trip over stale conflict state). Only stages; never
    commits."""
    if not is_repo(cwd):
        return
    _run(["add", "-A"], cwd)


def save_patch_ref(cwd: Path, task_id: str, step_id: str, patch_text: str) -> None:
    """Persist `patch_text` as a blob object pinned under a hidden ref
    (refs/harn/patches/<task_id>/<step_id>), mirroring how `checkpoint`
    pins a dangling commit. Best-effort; no-op on failure."""
    if not is_repo(cwd):
        return
    try:
        r = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=cwd, input=patch_text,
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return
    sha = r.stdout.strip()
    if r.returncode != 0 or not sha:
        return
    _run(["update-ref", f"refs/harn/patches/{task_id}/{step_id}", sha], cwd)


def load_patch_ref(cwd: Path, task_id: str, step_id: str) -> str:
    """Read back a patch saved with `save_patch_ref`, or '' if missing / not
    a repo / any git failure. Uses raw subprocess (not `_run`) so the patch
    text round-trips exactly, without `_run`'s trailing-whitespace strip."""
    if not is_repo(cwd):
        return ""
    try:
        r = subprocess.run(
            ["git", "cat-file", "-p", f"refs/harn/patches/{task_id}/{step_id}"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def clear_patch_refs(cwd: Path, task_id: str) -> None:
    if not is_repo(cwd):
        return
    code, out, _ = _run(["for-each-ref", "--format=%(refname)",
                         f"refs/harn/patches/{task_id}"], cwd)
    if code == 0:
        for ref in out.splitlines():
            if ref.strip():
                _run(["update-ref", "-d", ref.strip()], cwd)


def delete_patch_ref(cwd: Path, task_id: str, name: str) -> None:
    if is_repo(cwd):
        _run(["update-ref", "-d", f"refs/harn/patches/{task_id}/{name}"], cwd)


def commit_to_branch(cwd: Path, branch: str, message: str,
                     exclude: tuple[str, ...] = ()) -> str | None:
    """Stage the working-tree changes (minus `exclude` pathspecs) and, only if
    there's something to commit, create/checkout `branch` from HEAD and commit
    there. Returns the commit sha, or None (touching nothing beyond staging)
    if not a repo, nothing to commit, or any git step fails — harn's ONLY
    commit path, opt-in per role.

    HEAD is always restored to the branch this was called on: on success the
    commit lands on `branch` (which push_branch then pushes) while HEAD moves
    back to the original branch; on failure the branch switch (if any) is
    undone and a branch we created is deleted. This is a tool touching the
    human's real working repo (not an isolated worktree) — it must never
    leave the caller on a surprise branch."""
    if not is_repo(cwd) or not branch:
        return None
    code, orig, _ = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    orig = orig if code == 0 and orig else None
    pathspec = [".", *[f":(exclude){p}" for p in exclude]]
    _run(["add", "-A", "--", *pathspec], cwd)
    # nothing staged → nothing to commit; don't create/switch a branch.
    if _run(["diff", "--cached", "--quiet"], cwd)[0] == 0:
        return None
    # branch may already exist (a re-run) — checkout if so, else create.
    branch_existed = _run(["rev-parse", "--verify", "--quiet", branch], cwd)[0] == 0
    if branch_existed:
        switched = _run(["checkout", branch], cwd)[0] == 0
    else:
        switched = _run(["checkout", "-b", branch], cwd)[0] == 0
    if not switched:
        return None  # never left orig branch; staged index remains, harmless
    sha = None
    if _run(["commit", "-m", message], cwd)[0] == 0:
        code, out, _ = _run(["rev-parse", "HEAD"], cwd)
        sha = out if code == 0 else None
    # Restore HEAD to the original branch on every path past the checkout:
    # success leaves the commit safely on `branch`; failure undoes the switch.
    if orig:
        _run(["checkout", orig], cwd)
    if sha is None and not branch_existed:
        _run(["branch", "-D", branch], cwd)
    return sha


def push_branch(cwd: Path, remote: str, branch: str) -> bool:
    """Push `branch` to `remote` (best-effort). Returns success."""
    if not is_repo(cwd) or not remote or not branch:
        return False
    return _run(["push", "-u", remote, branch], cwd, timeout=60)[0] == 0
