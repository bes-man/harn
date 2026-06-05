"""Adapter registry. Each adapter only knows how to invoke one agent headless.

Everything else (tasks, skills, state, feedback, notifications) is shared and
agent-agnostic, exposed identically to every agent through the MCP server.
"""
from __future__ import annotations

from .base import Adapter, AgentResult
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .antigravity import AntigravityAdapter
from .qwen import QwenAdapter

_REGISTRY: dict[str, type[Adapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
    "antigravity": AntigravityAdapter,
    "qwen": QwenAdapter,
}


def get_adapter(name: str) -> Adapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"unknown agent '{name}'. Available: {', '.join(sorted(_REGISTRY))}"
        )


__all__ = ["Adapter", "AgentResult", "get_adapter"]
