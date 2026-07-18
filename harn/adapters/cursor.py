from __future__ import annotations

from pathlib import Path
import re

from .base import Adapter, AgentResult, EventCallback


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
    MODELS = ("auto", "gpt-5.3-codex", "composer-2.5", "claude-sonnet-5-medium",
              "gemini-3.1-pro")

    def discover_models(self) -> tuple[str, ...]:
        """Ask the authenticated Cursor CLI for this account's live catalog."""
        if not self.available():
            return tuple(self.MODELS)
        result = self._exec([self.binary, "models"], Path.cwd(), timeout=10)
        if not result.ok:
            return tuple(self.MODELS)
        models = []
        for line in result.stdout.splitlines():
            match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._:+-]*)\s+-\s+", line.strip())
            if match and match.group(1) not in models:
                models.append(match.group(1))
        return tuple(models) or tuple(self.MODELS)

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None,
                on_event: EventCallback | None = None) -> AgentResult:
        # Headless Cursor rejects newly-created wave workspaces unless trust is
        # explicit; these directories are created and populated by harn itself.
        argv = [self.binary, "-p", "--trust", prompt] + self._model_args(
            model, effort, temperature)
        return self._run_cli(argv, cwd, timeout, on_event=on_event)
