"""Shared document-intake path — both the Telegram and HTTP channels call
`intake()` to turn an uploaded file into a task, then (optionally) kick off
an agent behind a confirm gate.
"""
from __future__ import annotations

from pathlib import Path

from . import attachments, tasks
from . import triggers as triggers_mod
from .config import Config
from .telegram import TelegramHIL


def _confirm(env_dir: Path, cfg: Config, summary: str) -> bool:
    """Ask the human (via Telegram) whether to run the agent on the new task.

    Interprets a yes/y/да reply (case-insensitive) as True; anything else —
    including a decline, timeout, or an unanswered card — as False.

    If Telegram isn't configured, this defaults to True: a local/API caller
    that has `intake_confirm_before_run` on but no Telegram channel wired up
    has no way to ever answer the gate, so refusing to run would just hang
    the intake forever. Callers that want a real confirm gate need Telegram
    configured; without it we fail open rather than fail closed.

    Fully guarded — any failure talking to Telegram is treated as a decline
    unless we've already decided to default to True (unconfigured), so a
    broken Telegram integration never crashes `intake()`.

    `_confirm` must never hang: `await_answer` is always called with a bounded
    `timeout_s` (derived from `cfg.wait_timeout_minutes` when set, else a
    5-minute default), so a Telegram card that nobody answers still returns
    within a bounded time instead of blocking the calling HTTP request
    thread forever. A timeout / no answer fails safe to False (don't
    auto-run — the task and file are already saved, a human can run it
    manually later).
    """
    try:
        hil = TelegramHIL.from_env(env_dir)
        if hil is None:
            return True
        timeout_s = (
            cfg.wait_timeout_minutes * 60
            if getattr(cfg, "wait_timeout_minutes", 0) > 0
            else 300
        )
        reply, _source = hil.await_answer(
            f"{summary} — reply 'yes' to run",
            state_dir=env_dir / "state",
            timeout_s=timeout_s,
            remind_every_s=timeout_s // 2 or 1,
        )
        if not reply:
            return False
        return reply.strip().lower() in {"yes", "y", "да"}
    except Exception:
        return False


def intake(
    project_root: Path,
    env_dir: Path,
    *,
    filename: str,
    data: bytes,
    text: str = "",
    agent: str | None = None,
    cfg: Config | None = None,
) -> dict:
    """Create a new task from an uploaded document, attach the file, and —
    if `agent` is given — dispatch it to run (behind a confirm gate unless
    `intake_confirm_before_run` is False).
    """
    try:
        title = next((line.strip() for line in text.splitlines() if line.strip()), filename)
        task = tasks.create_task(env_dir, title, description=text)
        attachments.save(env_dir, task.id, filename, data)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    if not agent:
        return {"ok": True, "task_id": task.id}

    cfg = cfg or Config.load(env_dir)
    if cfg.intake_confirm_before_run:
        summary = f"New task {task.id} ({title!r}) from {filename!r} — run /{agent}?"
        if not _confirm(env_dir, cfg, summary):
            return {"ok": True, "task_id": task.id, "confirmed": False}

    result = triggers_mod.dispatch_command(project_root, env_dir, agent, task.id, cfg=cfg)
    if isinstance(result, dict):
        return {**result, "task_id": task.id}
    return {"ok": bool(result), "task_id": task.id}
