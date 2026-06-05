from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass
class AgentResult:
    ok: bool
    text: str
    # Token usage for the turn, when the agent CLI reports it (e.g. Claude's
    # --output-format json). None means "this agent didn't expose it".
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def usage_str(self) -> str:
        """Compact 'in+out tokens (~$cost)' for logs, or '' if unknown."""
        if self.total_tokens is None:
            return ""
        s = f"{self.input_tokens or 0}+{self.output_tokens or 0} tokens"
        if self.cost_usd is not None:
            s += f" (~${self.cost_usd:.4f})"
        return s


@dataclass
class _Exec:
    ok: bool
    stdout: str
    stderr: str
    timed_out: bool = False


class Adapter:
    """Base adapter. Subclasses run a single agent turn headless."""

    name = "base"
    binary = ""

    def available(self) -> bool:
        """Whether the underlying CLI/binary is installed and runnable."""
        return bool(self.binary) and shutil.which(self.binary) is not None

    def run_turn(self, prompt: str, cwd: Path) -> AgentResult:
        """Run one non-interactive turn with `prompt` in working dir `cwd`."""
        raise NotImplementedError

    def _exec(self, argv: Sequence[str], cwd: Path, timeout: int) -> _Exec:
        """Run the CLI and return raw streams separately (so a JSON-emitting
        adapter can parse clean stdout)."""
        try:
            proc = subprocess.run(
                list(argv),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return _Exec(proc.returncode == 0, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired:
            return _Exec(False, "", f"{self.name} timed out after {timeout}s", True)

    def _run_cli(
        self, argv: Sequence[str], cwd: Path, timeout: int = 1800
    ) -> AgentResult:
        """Shared headless invocation: shell out to a CLI and capture output.

        Adapters only differ in the argv they build; the subprocess handling
        (missing binary, timeout, merged stdout/stderr) is identical, so it
        lives here once.
        """
        if not self.available():
            return AgentResult(
                ok=False,
                text=f"{self.binary} CLI not found on PATH. Install {self.name} first.",
            )
        r = self._exec(argv, cwd, timeout)
        if r.timed_out:
            return AgentResult(ok=False, text=r.stderr)
        return AgentResult(ok=r.ok, text=r.stdout + r.stderr)
