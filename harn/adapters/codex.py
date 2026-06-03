from __future__ import annotations

import shutil
from pathlib import Path

from .base import Adapter, AgentResult


class CodexAdapter(Adapter):
    """Stub adapter for codex. Wire up headless invocation here.

    Intended entrypoint: codex exec.
    The shared logic (tasks, skills, ask_user, feedback, notifications) already
    works via the MCP server; only this run_turn needs implementing.
    """

    name = "codex"
    binary = "codex"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run_turn(self, prompt: str, cwd: Path) -> AgentResult:
        return AgentResult(
            ok=False,
            text="codex adapter not implemented yet (stub). "
                 "Implement run_turn() to invoke: codex exec",
        )
