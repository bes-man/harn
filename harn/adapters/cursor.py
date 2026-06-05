from __future__ import annotations

from pathlib import Path

from .base import Adapter, AgentResult


class CursorAdapter(Adapter):
    """Drives Cursor in headless mode via `cursor-agent -p`.

    Only shells out to the CLI the user already has installed; the shared
    logic (tasks, skills, ask_user, feedback, notifications) reaches the agent
    through the MCP server, so only this headless entrypoint is agent-specific.
    """

    name = "cursor"
    binary = "cursor-agent"

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800) -> AgentResult:
        # `-p/--print` makes cursor-agent run non-interactively and print output.
        return self._run_cli([self.binary, "-p", prompt], cwd, timeout)
