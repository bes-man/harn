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
    # `--model`/`--effort`/`--temperature` are the base class's best-effort
    # convention, unconfirmed for this specific CLI build — override the
    # *_FLAG class attrs above if your `antigravity` CLI uses different syntax.
    MODELS = ("gemini-3-pro", "gemini-3-flash", "gemini-3-deep-think")

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None) -> AgentResult:
        # `antigravity exec` runs a single non-interactive turn and prints output.
        argv = ([self.binary, "exec", prompt]
                + self._model_args(model, effort, temperature))
        return self._run_cli(argv, cwd, timeout)
