from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class AgentResult:
    ok: bool
    text: str


class Adapter:
    """Base adapter. Subclasses run a single agent turn headless."""

    name = "base"

    def available(self) -> bool:
        """Whether the underlying CLI/binary is installed and runnable."""
        raise NotImplementedError

    def run_turn(self, prompt: str, cwd: Path) -> AgentResult:
        """Run one non-interactive turn with `prompt` in working dir `cwd`."""
        raise NotImplementedError
