from __future__ import annotations

from pathlib import Path

from .base import Adapter, AgentResult


class AntigravityAdapter(Adapter):
    """Drives Antigravity in headless mode via its CLI (`antigravity exec`).

    Only shells out to the CLI the user already has installed; the shared
    logic (tasks, skills, ask_user, feedback, notifications) reaches the agent
    through the MCP server, so only this headless entrypoint is agent-specific.

    Note: Antigravity also ships a `google.antigravity` Python SDK. The CLI is
    used here to stay consistent with the other adapters (no hard SDK
    dependency, agent-agnostic core); swap `run_turn` to call the SDK if you
    prefer in-process invocation.
    """

    name = "antigravity"
    binary = "antigravity"

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800) -> AgentResult:
        # `antigravity exec` runs a single non-interactive turn and prints output.
        return self._run_cli([self.binary, "exec", prompt], cwd, timeout)
