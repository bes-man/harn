from __future__ import annotations

from pathlib import Path

from .base import Adapter, AgentResult


class CodexAdapter(Adapter):
    """Drives Codex in headless mode via `codex exec`.

    Like the Claude adapter, this only shells out to the CLI the user already
    has installed; the shared logic (tasks, skills, ask_user, feedback,
    notifications) reaches the agent through the MCP server, so only this
    headless entrypoint is agent-specific.
    """

    name = "codex"
    binary = "codex"

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800) -> AgentResult:
        # `codex exec` runs a single non-interactive turn and prints the result.
        return self._run_cli([self.binary, "exec", prompt], cwd, timeout)
