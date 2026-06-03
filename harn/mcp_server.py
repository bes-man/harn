"""harn MCP server — the universal capability layer.

Every supported agent (Claude, Codex, Cursor, Antigravity) speaks MCP, so the
same tools behave identically everywhere. Transport defaults to stdio (most
local: the agent spawns this as a subprocess); pass http=True to serve over
127.0.0.1 instead, which can later be exposed publicly behind auth/TLS.

The server acts on a single harn_env, resolved from $HARN_ENV_DIR or ./harn_env.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import skills as skills_mod
from . import state as state_mod
from . import tasks as tasks_mod
from .config import Config
from .feedback import run_feedback
from .notify import notify


def _env_dir() -> Path:
    return Path(os.environ.get("HARN_ENV_DIR", "harn_env")).resolve()


def build_server():
    from mcp.server.fastmcp import FastMCP  # imported lazily so core CLI has no hard dep

    mcp = FastMCP("harn")

    @mcp.tool()
    def list_skills() -> str:
        """List installed skills as 'name: description'. Cheap to read; use it to
        decide which skill bodies to pull in with read_skill (saves context)."""
        return skills_mod.index(_env_dir())

    @mcp.tool()
    def read_skill(name: str) -> str:
        """Return the full body of one skill. Call ONLY when the task needs it."""
        body = skills_mod.read_skill(_env_dir(), name)
        return body if body is not None else f"(no skill named '{name}')"

    @mcp.tool()
    def get_next_task() -> str:
        """Return the highest-priority unfinished task, or a note if none."""
        t = tasks_mod.next_task(_env_dir())
        if t is None:
            return "(no pending tasks)"
        return f"id={t.id}\ntitle={t.title}\n\n{t.path.read_text(encoding='utf-8')}"

    @mcp.tool()
    def ask_user(question: str) -> str:
        """Ask the human a clarifying question. Use whenever something is
        ambiguous, risky, or you're unsure. This pauses the loop, notifies the
        user (Telegram/Slack), and waits for `harn answer`. STOP after calling."""
        state_dir = _env_dir() / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_mod.blocked_marker(state_dir).write_text(question, encoding="utf-8")
        st = state_mod.State.load(state_dir)
        st.block(question)
        st.save(state_dir)
        channels = notify(f"[harn] Agent asks:\n{question}")
        return (
            "Question recorded; loop paused and user notified via "
            f"{channels or 'no configured channel'}. Stop now and wait."
        )

    @mcp.tool()
    def run_tests() -> str:
        """Run the project's configured feedback command and return the tail."""
        cfg = Config.load(_env_dir())
        fb = run_feedback(cfg.test_cmd, _env_dir().parent)
        return f"ran={fb.ran} ok={fb.ok}\n{fb.tail()}"

    @mcp.tool()
    def complete_task(task_id: str, summary: str = "") -> str:
        """Mark a task done (call only after tests pass)."""
        for t in tasks_mod.load_tasks(_env_dir()):
            if t.id == task_id:
                tasks_mod.mark_done(t, summary)
                return f"task '{task_id}' marked done"
        return f"(no task '{task_id}')"

    @mcp.tool()
    def status() -> str:
        """Current loop phase and pending question, if any."""
        st = state_mod.State.load(_env_dir() / "state")
        return f"phase={st.phase} task={st.current_task} question={st.question}"

    return mcp


def serve(http: bool = False, host: str = "127.0.0.1", port: int = 8765) -> None:
    mcp = build_server()
    if http:
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio
