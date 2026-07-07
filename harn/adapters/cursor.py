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
    # `--model`/`--effort`/`--temperature` are the base class's best-effort
    # convention, unconfirmed for this specific CLI build — override the
    # *_FLAG class attrs above if your `cursor-agent` version uses different
    # syntax (e.g. a short `-m` form).
    MODELS = ("composer-1", "sonnet-4.5", "opus-4.5", "gpt-5.1", "gemini-3-pro")

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None) -> AgentResult:
        # `-p/--print` makes cursor-agent run non-interactively and print output.
        argv = [self.binary, "-p", prompt] + self._model_args(model, effort, temperature)
        return self._run_cli(argv, cwd, timeout)
