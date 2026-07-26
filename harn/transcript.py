"""Persistent, provider-neutral per-step agent transcripts.

The adapter stream can be noisy and provider-specific.  This module stores
only the small visible event contract consumed by Studio, tagged with the harn
task/run/step attempt that produced it.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


_FILE = "step_transcript.jsonl"
_LOCK = threading.Lock()
_VISIBLE_KEYS = (
    "seq", "ts", "task_id", "step_id", "run_id", "attempt",
    "kind", "phase", "title", "text", "item_id",
)


def _path(env_dir: Path) -> Path:
    return env_dir / "state" / _FILE


def _rows(env_dir: Path) -> list[dict]:
    path = _path(env_dir)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and isinstance(row.get("seq"), int):
            rows.append(row)
    return rows


def latest_attempt(env_dir: Path, task_id: str, step_id: str) -> int:
    """The highest attempt already recorded for this step's feed, or 0.

    Studio groups a step's feed by attempt and collapses every group except
    the highest, so an entry written with a LOWER number lands inside a
    collapsed block instead of at the end where it just happened. Callers
    that append out-of-band (a question, a human's answer) use this to stay
    in the current group — they can't rely on the step ledger's `attempts`,
    which `loop.answer` deliberately resets to 0 to hand the step a fresh
    budget, and which would otherwise send the entry backwards.
    """
    best = 0
    for row in _rows(env_dir):
        if row.get("task_id") == task_id and row.get("step_id") == step_id:
            try:
                best = max(best, int(row.get("attempt") or 0))
            except (TypeError, ValueError):
                continue
    return best


def append(env_dir: Path, *, task_id: str, step_id: str, run_id: str,
           attempt: int, kind: str, phase: str, title: str = "",
           text: str = "", item_id: str = "") -> dict:
    """Append one visible transcript entry and return its stored record."""
    with _LOCK:
        seq = max((row["seq"] for row in _rows(env_dir)), default=0) + 1
        record = {
            "seq": seq,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "task_id": str(task_id),
            "step_id": str(step_id),
            "run_id": str(run_id),
            "attempt": max(1, int(attempt)),
            "kind": str(kind),
            "phase": str(phase),
            "title": str(title or ""),
            "text": str(text or ""),
            "item_id": str(item_id or ""),
        }
        try:
            path = _path(env_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return record


def read(env_dir: Path, *, task_id: str, step_id: str | None = None,
         after: int = 0, limit: int = 500) -> dict:
    """Return normalized entries after ``after`` and their newest cursor."""
    safe_after = max(0, int(after))
    safe_limit = min(500, max(1, int(limit)))
    matches = [
        row for row in _rows(env_dir)
        if row.get("task_id") == task_id
        and (step_id is None or row.get("step_id") == step_id)
        and row["seq"] > safe_after
    ][:safe_limit]
    visible = [{key: row.get(key, "") for key in _VISIBLE_KEYS} for row in matches]
    cursor = visible[-1]["seq"] if visible else safe_after
    return {"entries": visible, "cursor": cursor}


def clear_task(env_dir: Path, task_id: str) -> int:
    """Atomically remove one task's transcript entries, preserving all others."""
    with _LOCK:
        path = _path(env_dir)
        if not path.exists():
            return 0
        rows = _rows(env_dir)
        kept = [row for row in rows if row.get("task_id") != task_id]
        removed = len(rows) - len(kept)
        if not removed:
            return 0
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)
            return 0
        return removed
