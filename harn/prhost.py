"""PR-host seam: open a pull request for a pushed branch. `gh` (GitHub)
is the one implementation; GitLab/others are future one-file providers
(mirrors trackers.py). Never raises — a missing/unauthenticated gh returns
None so a role run's PR step degrades to 'branch pushed, no PR'. See
docs/superpowers/specs/2026-07-19-push-pr-and-authenticated-api-design.md.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_URL_RE = re.compile(r"https?://\S+/pull/\d+")


def create_pr(cwd: Path, *, base: str, head: str, title: str, body: str) -> str | None:
    """Open a GitHub PR via `gh pr create`. Returns the PR URL, or None on
    any failure (gh absent, not authenticated, non-zero exit)."""
    argv = ["gh", "pr", "create", "--base", base, "--head", head,
            "--title", title, "--body", body]
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    m = _URL_RE.search(r.stdout or "")
    return m.group(0) if m else (r.stdout or "").strip() or None
