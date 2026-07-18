"""Structured, append-only event stream — the observability spine of harn.

`progress.py` is the *human* narrative (free-text lines an agent reads on
pickup). This is its machine-readable twin: one JSON object per line in
`harn_env/state/events.jsonl`, stamped with a `run_id` so every stage of one
`harn run` / chat session correlates. It is RUNTIME telemetry, never injected
into a prompt — so it costs zero context tokens while making each run traceable.

Readers (`harn trace`, `harn metrics`) reconstruct what happened: stages, their
verdicts, token cost, durations, blocks, errors. Pure stdlib — same
dependency-free footprint as progress.py.

Event vocabulary (the `event` field):
  run_start    — a run/session began (kind=loop|chat|watch)
  stage_start  — an agent turn is about to run (stage=execute|verify|oracle|…)
  stage_end    — that turn finished (carries verdict, tok_in/out, dur_ms, summary)
  gate_skipped — a configured stage was skipped (reason=…)
  block        — the agent raised a question (BLOCKED)
  answer       — a human answer landed (source=chat|telegram|auto|cli)
  cycle_end    — a task work-cycle completed (submitted for review / done)
  error        — a turn raised (detail=…)
  run_end      — the run/session finished (phase=…)
  context_read — a skill/service/PRD/guidance body was pulled into a turn's
                 context (kind=skill|service|prd|guidance, name=…)
  tool_used    — any MCP tool was invoked during a task's claimed turn
                 (tool=…, step_id=… when known)
  config_error — a workflow declaration harn can't safely honor was ignored
                 (detail=…)
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_FILE = "events.jsonl"
_RUN_ID_FILE = ".run_id"
_LOCK = threading.Lock()


def _state_dir(env_dir: Path) -> Path:
    return env_dir / "state"


def _events_path(env_dir: Path) -> Path:
    return _state_dir(env_dir) / _FILE


def _run_id_path(env_dir: Path) -> Path:
    return _state_dir(env_dir) / _RUN_ID_FILE


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run(env_dir: Path, kind: str = "run") -> str:
    """Start a correlation scope: mint a run_id, persist it, emit `run_start`.

    Call once at the top of `harn run` and once when the MCP/chat session or the
    watch dispatcher starts. Every `emit` until the next `new_run` is stamped
    with this id, so a whole run's events join up.
    """
    rid = "r-" + uuid.uuid4().hex[:8]
    sd = _state_dir(env_dir)
    sd.mkdir(parents=True, exist_ok=True)
    _run_id_path(env_dir).write_text(rid, encoding="utf-8")
    emit(env_dir, "run_start", kind=kind)
    return rid


def current_run(env_dir: Path) -> str:
    """The active run_id (or '' if none has been started yet)."""
    p = _run_id_path(env_dir)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def emit(env_dir: Path, event: str, **fields) -> None:
    """Append one event line. `None`-valued fields are dropped so rows stay lean.

    Best-effort: telemetry must never crash the run, so any I/O error is
    swallowed (the work matters more than the log of it).
    """
    rec: dict = {"ts": _now_iso(), "run_id": current_run(env_dir), "event": event}
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    try:
        p = _events_path(env_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(env_dir: Path, *, task_id: str | None = None,
         run_id: str | None = None) -> list[dict]:
    """Load events, optionally filtered by task and/or run. Skips malformed
    lines rather than failing — a partial trace beats no trace."""
    p = _events_path(env_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if task_id and rec.get("task_id") != task_id:
            continue
        if run_id and rec.get("run_id") != run_id:
            continue
        out.append(rec)
    return out


def clear_task(env_dir: Path, task_id: str) -> int:
    """Atomically remove runtime telemetry belonging to one task."""
    with _LOCK:
        path = _events_path(env_dir)
        if not path.exists():
            return 0
        rows = read(env_dir)
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


def metrics(env_dir: Path, *, run_id: str | None = None) -> dict:
    """Aggregate events into headline numbers for observability.

    Returns counts of runs/cycles/blocks/errors, per-stage turn counts +
    summed tokens + mean duration, and the oracle verdict distribution.
    """
    evs = read(env_dir, run_id=run_id)
    stages: dict[str, dict] = {}
    oracle = {"PASS": 0, "FAIL": 0, "DEBT": 0}
    runs = cycles = blocks = errors = 0
    tok_in = tok_out = 0
    cost = 0.0
    for e in evs:
        ev = e.get("event")
        if ev == "run_start":
            runs += 1
        elif ev == "cycle_end":
            cycles += 1
        elif ev == "block":
            blocks += 1
        elif ev == "error":
            errors += 1
        elif ev == "stage_end":
            s = stages.setdefault(e.get("stage", "?"),
                                  {"count": 0, "tok": 0, "dur_ms": 0})
            s["count"] += 1
            s["tok"] += (e.get("tok_in") or 0) + (e.get("tok_out") or 0)
            s["dur_ms"] += e.get("dur_ms") or 0
            tok_in += e.get("tok_in") or 0
            tok_out += e.get("tok_out") or 0
            cost += e.get("cost_usd") or 0.0
            v = e.get("verdict")
            if e.get("stage") == "oracle" and v in oracle:
                oracle[v] += 1
    for s in stages.values():
        s["mean_dur_ms"] = round(s["dur_ms"] / s["count"]) if s["count"] else 0
    return {
        "runs": runs, "cycles": cycles, "blocks": blocks, "errors": errors,
        "tok_in": tok_in, "tok_out": tok_out, "cost_usd": round(cost, 4),
        "stages": stages, "oracle": oracle, "events": len(evs),
    }
