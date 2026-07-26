from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from . import base as base_mod
from .base import Adapter, AgentResult, EventCallback

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

    We request Claude Code's documented ``stream-json`` print-mode format so
    activity reaches the run sidebar while the agent is working, rather than
    waiting for one buffered JSON record at turn completion.
    """

    name = "claude"
    binary = "claude"
    # Claude Code's `-p` mode has no sampling temperature to set (it's fixed for
    # tool-use reliability); "effort" isn't a plain CLI flag either (extended
    # thinking is a model/API concept, not exposed this way headless) — both
    # unconfirmed, so left as the base class's best-effort default. `--model`
    # is real and documented for the Claude Code CLI.
    # Aliases first: they resolve to whatever is current for the account, so
    # they don't go stale the way a pinned id does. The pinned ids follow for
    # steps that must not drift between runs. The Claude CLI exposes no
    # "list models" command, so unlike codex/cursor this list can't be
    # discovered at runtime — it needs updating by hand when the family moves.
    MODELS = ("opus", "sonnet", "haiku", "opusplan",
              "claude-opus-5", "claude-sonnet-5", "claude-fable-5",
              "claude-haiku-4-5-20251001")
    EFFORTS = ()          # no confirmed --effort flag for `claude -p` (see above)
    TEMPERATURES = ()     # no sampling temperature exposed (see above)

    def auth_status(self) -> tuple[str, str]:
        """Ask the CLI itself whether it is signed in — free and definitive.

        `claude auth status --json` is the CLI's own answer (~0.3s, no turn,
        no tokens), so it stays correct across every auth method the CLI
        supports. That matters concretely: `claude setup-token` stores a
        long-lived subscription token in a different shape than an
        interactive login, and an earlier version of this check that parsed
        ~/.claude/.credentials.json by hand would have reported a perfectly
        good setup-token login as "unknown".

        Why check at all: diagnosed live on a machine where the Claude
        DESKTOP app worked fine — it refreshes its token in-process and never
        writes it out — while every `claude -p` subprocess failed, because
        the CLI itself was logged out. "Claude works in my console" and
        "harn can run Claude" are genuinely different questions; harn always
        shells out, so only the CLI's own state answers the second. Without
        this the only way to find out is to launch a run and watch it die.
        """
        import json as _json
        binary = base_mod.resolve_binary(self.binary)
        if not binary:
            return ("unknown", "")
        result = self._exec([binary, "auth", "status", "--json"],
                            Path.cwd(), timeout=15)
        try:
            data = _json.loads(result.stdout.strip())
        except (ValueError, AttributeError):
            # An older CLI without the subcommand, or unparseable output.
            # "unknown" rather than crying wolf on a working setup.
            return ("unknown", "")
        if not isinstance(data, dict) or "loggedIn" not in data:
            return ("unknown", "")
        if data.get("loggedIn"):
            return ("ok", str(data.get("authMethod") or ""))
        return ("expired",
                f"the {self.name} CLI is not signed in "
                f"(auth status: {data.get('authMethod') or 'none'})")

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None,
                on_event: EventCallback | None = None) -> AgentResult:
        binary = base_mod.resolve_binary(self.binary)
        if not binary:
            result = AgentResult(
                ok=False,
                text="claude CLI not found on PATH. Install claude first.",
            )
            if on_event:
                on_event({"kind": "error", "phase": "failed",
                          "title": self.name, "text": result.text})
            return result
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
        argv = ([binary, "-p", prompt, "--output-format", "stream-json",
                 "--verbose", "--permission-mode", "bypassPermissions"]
                + self._model_args(model, effort, temperature))
        if on_event:
            on_event({"kind": "status", "phase": "started",
                      "title": self.name, "text": "Agent started"})
        result = self._run_stream(argv, cwd, timeout, on_event)
        if on_event and result.text.strip():
            on_event({"kind": "message" if result.ok else "error",
                      "phase": "completed" if result.ok else "failed",
                      "title": self.name, "text": result.text.strip()})
        return result

    def _run_stream(self, argv: list[str], cwd: Path, timeout: int,
                    on_event: EventCallback | None) -> AgentResult:
        """Consume Claude's JSONL print stream without holding UI progress.

        ``stream-json`` emits assistant/tool records before the terminal
        ``result`` record. A pair of reader threads keeps stderr drained and
        lets the timeout still fire when Claude produces no output.
        """
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            return AgentResult(False, str(exc))
        lines: queue.Queue[str | None] = queue.Queue()
        errors: list[str] = []

        def read_stdout() -> None:
            if proc.stdout is not None:
                for line in proc.stdout:
                    lines.put(line)
            lines.put(None)

        def read_stderr() -> None:
            if proc.stderr is not None:
                errors.extend(proc.stderr)

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        deadline = time.monotonic() + timeout
        final: dict = {}
        raw: list[str] = []
        timed_out = False
        name_by_id: dict[str, str] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                proc.kill()
                break
            try:
                line = lines.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if line is None:
                break
            raw.append(line)
            try:
                data = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if data.get("type") == "result":
                final = data
            for event in self._normalize_event(data, name_by_id):
                if on_event:
                    on_event(event)
        try:
            returncode = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = -9
            timed_out = True
        stderr = "".join(errors).strip()
        if timed_out:
            return AgentResult(False, f"claude timed out after {timeout}s")
        if final:
            parsed = self._parse(True, json.dumps(final), stderr)
            return AgentResult(returncode == 0 and parsed.ok, parsed.text,
                               parsed.input_tokens, parsed.output_tokens,
                               parsed.cost_usd, parsed.cache_read_tokens)
        return AgentResult(returncode == 0, "".join(raw).strip() or stderr)

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
