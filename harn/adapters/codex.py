from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from . import base as base_mod
from .base import Adapter, AgentResult, EventCallback


class CodexAdapter(Adapter):
    """Drives Codex in headless mode via `codex exec`.

    Like the Claude adapter, this only shells out to the CLI the user already
    has installed; the shared logic (tasks, skills, ask_user, feedback,
    notifications) reaches the agent through the MCP server, so only this
    headless entrypoint is agent-specific.
    """

    name = "codex"
    binary = "codex"
    MODELS: tuple[str, ...] = ()
    EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
    TEMPERATURE_FLAG = None

    def discover_models(self) -> tuple[str, ...]:
        """Read the catalog authorized for the CLI's current account."""
        result = self._exec([self.binary, "debug", "models"], Path.cwd(), timeout=10)
        if not result.ok:
            return self.MODELS
        try:
            catalog = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            return self.MODELS
        models = catalog.get("models") if isinstance(catalog, dict) else None
        if not isinstance(models, list):
            return self.MODELS
        slugs: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or item.get("model") or item.get("id") or "").strip()
            if slug and slug not in slugs:
                slugs.append(slug)
        return tuple(slugs) or self.MODELS

    def _model_args(self, model: str | None = None, effort: str | None = None,
                    temperature: str | None = None) -> list[str]:
        legacy = re.fullmatch(
            r"(gpt-5\.6-(?:sol|terra|luna))-(low|medium|high|xhigh|max|ultra)",
            model or "",
        )
        if legacy:
            model = legacy.group(1)
            effort = effort or legacy.group(2)
        args = ["--model", model] if model else []
        if effort:
            args += ["-c", f'model_reasoning_effort="{effort}"']
        return args

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None,
                on_event: EventCallback | None = None) -> AgentResult:
        """Run Codex and consume its public JSONL event stream live."""
        resolved = base_mod.resolve_binary(self.binary)
        binary = self.binary if base_mod.shutil.which(self.binary) else resolved
        if not binary:
            result = AgentResult(False, "codex CLI not found on PATH. Install codex first.")
            if on_event:
                on_event({"kind": "error", "phase": "failed",
                          "title": self.name, "text": result.text})
            return result
        argv = ([binary, "exec", "--json", prompt]
                + self._model_args(model, effort, temperature))
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
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
        final_messages: list[str] = []
        usage: dict = {}
        timed_out = False
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
            try:
                data = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if data.get("type") == "turn.completed":
                usage = data.get("usage") or {}
            for event in self._normalize_event(data):
                if event["kind"] == "message" and event["phase"] == "completed":
                    final_messages.append(event["text"])
                if on_event:
                    on_event(event)
        returncode = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        stderr = "".join(errors).strip()
        if timed_out:
            text = f"codex timed out after {timeout}s"
            if on_event:
                on_event({"kind": "error", "phase": "failed",
                          "title": self.name, "text": text})
            return AgentResult(False, text)
        ok = returncode == 0
        text = "\n\n".join(final_messages).strip() or stderr
        if not ok and on_event and stderr:
            on_event({"kind": "error", "phase": "failed",
                      "title": self.name, "text": stderr})
        return AgentResult(
            ok=ok, text=text,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cached_input_tokens"),
        )

    @staticmethod
    def _normalize_event(data: dict) -> list[dict]:
        """Map public ``codex exec --json`` items to harn-visible events."""
        event_type = data.get("type") or ""
        if event_type == "turn.failed":
            error = data.get("error") or {}
            text = error.get("message") if isinstance(error, dict) else str(error)
            return [{"kind": "error", "phase": "failed", "title": "Codex",
                     "text": text or "Turn failed", "item_id": ""}]
        if not event_type.startswith("item.") or not isinstance(data.get("item"), dict):
            return []
        item = data["item"]
        item_type = item.get("type") or ""
        phase = {"item.started": "started", "item.updated": "updated",
                 "item.completed": "completed"}.get(event_type, "updated")
        item_id = str(item.get("id") or "")

        def visible(kind: str, title: str, text) -> list[dict]:
            if isinstance(text, (dict, list)):
                text = json.dumps(text, ensure_ascii=False)
            return [{"kind": kind, "phase": phase, "title": str(title or ""),
                     "text": str(text or ""), "item_id": item_id}]

        if item_type == "agent_message":
            return visible("message", "Agent", item.get("text"))
        if item_type == "reasoning":
            summary = item.get("summary") or ""
            if isinstance(summary, list):
                summary = "\n".join(str(x) for x in summary)
            return visible("status", "Reasoning", summary)
        if item_type == "command_execution":
            command = item.get("command") or "Command"
            output = item.get("aggregated_output") or item.get("output") or ""
            return visible("command", command, output)
        if item_type == "file_change":
            changes = item.get("changes") or []
            text = "\n".join(str(c.get("path") or c) if isinstance(c, dict) else str(c)
                             for c in changes)
            return visible("file_change", "Files changed", text)
        if item_type in ("mcp_tool_call", "collab_tool_call"):
            server = item.get("server") or item.get("receiver") or ""
            tool = item.get("tool") or item.get("name") or item_type.replace("_call", "")
            result = item.get("result")
            if result is None:
                result = item.get("error") or item.get("arguments") or ""
            return visible("tool", "/".join(x for x in (server, tool) if x), result)
        if item_type == "web_search":
            return visible("tool", "Web search", item.get("query") or item.get("result"))
        return []
