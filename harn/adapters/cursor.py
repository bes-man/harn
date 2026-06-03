from __future__ import annotations

import shutil
from pathlib import Path

from .base import Adapter, AgentResult


class CursorAdapter(Adapter):
    """Stub adapter for cursor. Wire up headless invocation here.

    Intended entrypoint: cursor-agent headless run.
    The shared logic (tasks, skills, ask_user, feedback, notifications) already
    works via the MCP server; only this run_turn needs implementing.
    """

    name = "cursor"
    binary = "cursor-agent"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run_turn(self, prompt: str, cwd: Path) -> AgentResult:
        return AgentResult(
            ok=False,
            text="cursor adapter not implemented yet (stub). "
                 "Implement run_turn() to invoke: cursor-agent headless run",
        )
