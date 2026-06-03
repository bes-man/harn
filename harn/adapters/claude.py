from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import Adapter, AgentResult


class ClaudeAdapter(Adapter):
    """Drives Claude Code in headless mode via `claude -p`.

    No hard dependency on the Agent SDK: harn just invokes the CLI the user
    already has installed, which keeps the harness agent-agnostic.
    """

    name = "claude"
    binary = "claude"

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800) -> AgentResult:
        if not self.available():
            return AgentResult(
                ok=False,
                text="claude CLI not found on PATH. Install Claude Code first.",
            )
        try:
            proc = subprocess.run(
                [self.binary, "-p", prompt],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            return AgentResult(ok=proc.returncode == 0, text=out)
        except subprocess.TimeoutExpired:
            return AgentResult(ok=False, text=f"claude timed out after {timeout}s")
