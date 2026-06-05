from __future__ import annotations

from pathlib import Path

from .base import Adapter, AgentResult


class QwenAdapter(Adapter):
    """Drives Qwen Code in headless mode via `qwen -p`.

    Qwen Code (the `qwen` CLI, adapted from the Gemini CLI) speaks MCP, so the
    shared brain (tasks, skills, ask_user, feedback, review, notifications)
    reaches it through the harn MCP server — only this headless entrypoint is
    agent-specific. It reads context from QWEN.md, but harn folds AGENTS.md into
    the prompt itself, so no extra wiring is needed.

    Note: `-p/--prompt` is the non-interactive flag; if your Qwen Code build
    differs, swap the one line below.
    """

    name = "qwen"
    binary = "qwen"

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800) -> AgentResult:
        return self._run_cli([self.binary, "-p", prompt], cwd, timeout)
