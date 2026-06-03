from __future__ import annotations

import shutil
from pathlib import Path

from .base import Adapter, AgentResult


class AntigravityAdapter(Adapter):
    """Stub adapter for antigravity. Wire up headless invocation here.

    Intended entrypoint: antigravity CLI / google.antigravity SDK.
    The shared logic (tasks, skills, ask_user, feedback, notifications) already
    works via the MCP server; only this run_turn needs implementing.
    """

    name = "antigravity"
    binary = "antigravity"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run_turn(self, prompt: str, cwd: Path) -> AgentResult:
        return AgentResult(
            ok=False,
            text="antigravity adapter not implemented yet (stub). "
                 "Implement run_turn() to invoke: antigravity CLI / google.antigravity SDK",
        )
