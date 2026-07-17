from __future__ import annotations

import json
from pathlib import Path

from .base import Adapter, AgentResult

# Matches the existing step_results.output truncation convention
# ((result.text or "")[-4000:] in loop.py) — capped so a noisy tool (e.g. a
# full test-suite dump) can't flood a captured tool_result; the marker keeps
# the truncation visible rather than silent.
_TOOL_RESULT_CAP = 4000


def _cap(text: str, limit: int = _TOOL_RESULT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


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
    # Claude Code's `-p` mode has no sampling temperature to set (it's fixed for
    # tool-use reliability); "effort" isn't a plain CLI flag either (extended
    # thinking is a model/API concept, not exposed this way headless) — both
    # unconfirmed, so left as the base class's best-effort default. `--model`
    # is real and documented for the Claude Code CLI.
    MODELS = ("opus", "sonnet", "haiku", "opusplan",
              "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5-20251001")
    EFFORTS = ()          # no confirmed --effort flag for `claude -p` (see above)
    TEMPERATURES = ()     # no sampling temperature exposed (see above)

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None) -> AgentResult:
        if not self.available():
            return AgentResult(
                ok=False,
                text="claude CLI not found on PATH. Install claude first.",
            )
        # `--permission-mode bypassPermissions` is REQUIRED for headless
        # operation: in `-p` mode there is no human to approve a tool call, so
        # without this every MCP/Bash/Edit tool the agent tries to use is
        # silently denied and the turn burns its whole budget writing "I need
        # permission to call these tools" instead of doing the work (confirmed
        # live: a task needing two custom MCP tools produced ZERO tool_used
        # events across many runs, each turn ending with a permission plea).
        # harn is by definition an autonomous background runner — the studio's
        # own Launch dialog says "an agent will start making changes in the
        # background" — so bypassing the interactive gate is the correct trust
        # model here, not a shortcut. (Per Claude Code docs this is the CLI
        # equivalent of the deprecated --dangerously-skip-permissions.)
        argv = ([self.binary, "-p", prompt, "--output-format", "json",
                 "--permission-mode", "bypassPermissions"]
                + self._model_args(model, effort, temperature))
        r = self._exec(argv, cwd, timeout)
        if r.timed_out:
            return AgentResult(ok=False, text=r.stderr)
        return self._parse(r.ok, r.stdout, r.stderr)

    @staticmethod
    def _normalize_event(data: dict, name_by_id: dict[str, str]) -> list[dict]:
        """Map a Claude Code `stream-json` print-mode record to the common
        transcript UI's event shape.

        `type: "assistant"` records carry the model's text and tool-call
        *names* (already handled). `type: "user"` records carry `tool_result`
        blocks — the actual RETURN VALUE of every tool call (a skill body, a
        weather JSON, anything) — previously dropped entirely, so the task's
        Context capture could only ever say a tool was called, never show
        what it returned. `name_by_id` is a small id→name map the caller keeps
        across one turn's events: assistant `tool_use` items register their
        id/name here as they're seen, so the later `tool_result` (which only
        carries `tool_use_id`) can resolve back to the tool that produced it.
        """
        t = data.get("type")
        if t == "assistant":
            return ClaudeAdapter._normalize_assistant(data, name_by_id)
        if t == "user":
            return ClaudeAdapter._normalize_tool_result(data, name_by_id)
        return []

    @staticmethod
    def _normalize_assistant(data: dict, name_by_id: dict[str, str]) -> list[dict]:
        message = data.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else []
        if not isinstance(content, list):
            return []
        events: list[dict] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                events.append({"kind": "message", "phase": "updated",
                               "title": "Claude", "text": str(item["text"])})
            elif item.get("type") == "tool_use":
                name = str(item.get("name") or "Tool")
                tool_id = item.get("id")
                if tool_id:
                    name_by_id[str(tool_id)] = name
                events.append({"kind": "tool", "phase": "started",
                               "title": name, "text": ""})
        return events

    @staticmethod
    def _normalize_tool_result(data: dict, name_by_id: dict[str, str]) -> list[dict]:
        message = data.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else []
        if not isinstance(content, list):
            return []
        events: list[dict] = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            text = ClaudeAdapter._tool_result_text(item)
            if not text:
                continue
            tool_id = str(item.get("tool_use_id") or "")
            name = name_by_id.get(tool_id, "Tool")
            events.append({"kind": "tool_result", "phase": "completed",
                           "title": name, "text": _cap(text)})
        return events

    @staticmethod
    def _tool_result_text(item: dict) -> str:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(str(c.get("text") or ""))
                elif isinstance(c, str):
                    parts.append(c)
            return "\n".join(parts)
        return ""

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
            cache_read_tokens=usage.get("cache_read_input_tokens"),
        )
