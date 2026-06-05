from __future__ import annotations

import json
from pathlib import Path

from .base import Adapter, AgentResult


class ClaudeAdapter(Adapter):
    """Drives Claude Code in headless mode via `claude -p`.

    No hard dependency on the Agent SDK: harn just invokes the CLI the user
    already has installed, which keeps the harness agent-agnostic.

    We request `--output-format json` so we can also report the turn's token
    usage (input/output tokens + cost), which harn records on the task. If the
    JSON can't be parsed for any reason, we fall back to the raw text and simply
    report no usage — never failing the turn over telemetry.
    """

    name = "claude"
    binary = "claude"

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800) -> AgentResult:
        if not self.available():
            return AgentResult(
                ok=False,
                text="claude CLI not found on PATH. Install claude first.",
            )
        r = self._exec(
            [self.binary, "-p", prompt, "--output-format", "json"], cwd, timeout
        )
        if r.timed_out:
            return AgentResult(ok=False, text=r.stderr)
        return self._parse(r.ok, r.stdout, r.stderr)

    @staticmethod
    def _parse(ok: bool, stdout: str, stderr: str) -> AgentResult:
        try:
            data = json.loads(stdout.strip())
        except (json.JSONDecodeError, ValueError):
            # Not JSON (older CLI, error text, etc.) — degrade gracefully.
            return AgentResult(ok=ok, text=stdout + stderr)
        if not isinstance(data, dict):
            return AgentResult(ok=ok, text=stdout + stderr)
        text = str(data.get("result") or data.get("text") or "").strip() or stderr
        usage = data.get("usage") or {}
        inp = usage.get("input_tokens")
        # Cache tokens count toward input if present.
        for k in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if usage.get(k):
                inp = (inp or 0) + usage[k]
        return AgentResult(
            ok=ok and not data.get("is_error", False),
            text=text,
            input_tokens=inp,
            output_tokens=usage.get("output_tokens"),
            cost_usd=data.get("total_cost_usd"),
        )
