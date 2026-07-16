"""Cross-process singleton lock via a PID file.

Every long-lived harn background process that must never run twice for the
same env_dir at once (`harn watch`, `harn ui`, an MCP-launched `harn run`)
uses this same tiny pattern: before starting, check whether a PREVIOUS PID
file still names a live process; if so, refuse instead of racing a second
instance against the same harn_env.

This started as three independent copy-pasted implementations (`watch.pid`
in mcp_server.py, `ui_run.pid` in runner.py, `ui_mcp.pid` in studio.py)
before a real incident: duplicate `harn watch`/`harn ui` processes for the
same project accumulated over days -- one auto-started from an MCP session,
another started by hand without checking -- each independently polling and
dispatching the same tasks. Consolidated here so every new long-lived
process gets the same protection for free, and the CLI entry points
(`harn watch`, `harn ui`) that never had this check at all can now share it
with the auto-start path instead of leaving it as an MCP-only safeguard.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def read_alive_json(marker_file: Path) -> dict | None:
    """Like `read_alive_pid`, but for a JSON marker carrying extra fields
    (e.g. host/port) alongside a `"pid"` key -- used where a caller needs to
    know not just THAT another instance is running, but where to reach it.

    Returns the parsed dict if `data["pid"]` is alive, else None (cleaning
    up the file)."""
    if not marker_file.exists():
        return None
    try:
        data = json.loads(marker_file.read_text(encoding="utf-8"))
        os.kill(int(data["pid"]), 0)   # raises if dead
        return data
    except (ProcessLookupError, ValueError, OSError, KeyError, TypeError,
            json.JSONDecodeError):
        marker_file.unlink(missing_ok=True)
        return None


def claim_json(marker_file: Path, data: dict) -> None:
    """Write `data` (must include a `"pid"` key) to `marker_file`, claiming
    it for the calling process. Caller must have already confirmed via
    `read_alive_json()` that nothing else currently holds it."""
    marker_file.parent.mkdir(parents=True, exist_ok=True)
    marker_file.write_text(json.dumps(data), encoding="utf-8")


def read_alive_pid(pid_file: Path) -> int | None:
    """The PID recorded in `pid_file`, if that process is still alive.

    Best-effort: removes the file itself if it's missing, corrupt, or names
    a process that's gone -- a crashed or killed process must never
    permanently block a future one from starting."""
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)   # raises if dead
        return pid
    except (ProcessLookupError, ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return None


def claim(pid_file: Path, pid: int | None = None) -> None:
    """Write `pid` (default: our own) to `pid_file`, claiming it for the
    calling process. Caller must have already confirmed via
    `read_alive_pid()` that nothing else currently holds it."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid if pid is not None else os.getpid()),
                        encoding="utf-8")


def release(pid_file: Path) -> None:
    """Remove our claim. Call on clean shutdown (KeyboardInterrupt, normal
    return) so the next start doesn't have to wait out a stale-pid check."""
    pid_file.unlink(missing_ok=True)
