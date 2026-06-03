"""Feedback loop: run the project's configured test command.

The command is language-agnostic and set per project in harn.toml
([feedback] test_cmd). harn's OWN tests are pytest, but a target project may
use anything (npm test, go test, cargo test, ...).
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FeedbackResult:
    ran: bool
    ok: bool
    output: str

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.output.splitlines()[-n:])


def run_feedback(test_cmd: str, cwd: Path, timeout: int = 600) -> FeedbackResult:
    if not test_cmd.strip():
        return FeedbackResult(ran=False, ok=True, output="(no test_cmd configured)")
    try:
        proc = subprocess.run(
            shlex.split(test_cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return FeedbackResult(ran=True, ok=proc.returncode == 0, output=out)
    except subprocess.TimeoutExpired:
        return FeedbackResult(ran=True, ok=False, output=f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return FeedbackResult(ran=True, ok=False, output=f"command not found: {exc}")
