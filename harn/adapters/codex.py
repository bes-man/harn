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
    # `--model`/`--effort`/`--temperature` are the base class's best-effort
    # convention, unconfirmed for this specific CLI build — override the
    # *_FLAG class attrs above if your `codex` version uses different syntax
    # (e.g. `-c key=value` config overrides instead of plain flags).
    MODELS = ("gpt-5.1-codex", "gpt-5.1-codex-mini", "gpt-5-codex", "o4-mini")

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None) -> AgentResult:
        # `codex exec` runs a single non-interactive turn and prints the result.
        argv = ([self.binary, "exec", prompt]
                + self._model_args(model, effort, temperature))
        return self._run_cli(argv, cwd, timeout)
