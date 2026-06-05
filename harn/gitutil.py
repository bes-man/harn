"""Thin git helpers for per-task baselines and rollback.

harn never *requires* git — every function degrades gracefully (returns "" / a
disabled result) when the project isn't a git repo or git isn't installed.
"""
from __future__ import annotations

import subprocess
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
