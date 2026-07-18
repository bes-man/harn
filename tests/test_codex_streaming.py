import json
import threading
import time
from pathlib import Path

from harn.adapters.codex import CodexAdapter


def test_codex_model_and_reasoning_effort_are_separate_cli_options():
    args = CodexAdapter()._model_args("gpt-5.6-luna", "high", "0.7")
    assert args == ["--model", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"']


def test_codex_migrates_legacy_reasoning_suffix_from_saved_workflows():
    args = CodexAdapter()._model_args("gpt-5.6-luna-high", None, None)
    assert args == ["--model", "gpt-5.6-luna", "-c", 'model_reasoning_effort="high"']


def test_codex_normalizes_visible_jsonl_items():
    cases = [
        ({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "Finished"}},
         ("message", "completed", "Finished")),
        ({"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "summary": "Inspecting tests"}},
         ("status", "completed", "Inspecting tests")),
        ({"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "pytest -q", "status": "in_progress"}},
         ("command", "started", "pytest -q")),
        ({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "pytest -q", "aggregated_output": "3 passed", "exit_code": 0, "status": "completed"}},
         ("command", "completed", "3 passed")),
        ({"type": "item.completed", "item": {"id": "t1", "type": "mcp_tool_call", "server": "harn", "tool": "board", "result": {"ok": True}, "status": "completed"}},
         ("tool", "completed", '{"ok": true}')),
        ({"type": "item.completed", "item": {"id": "f1", "type": "file_change", "changes": [{"path": "harn/studio.py", "kind": "update"}], "status": "completed"}},
         ("file_change", "completed", "harn/studio.py")),
    ]
    for raw, expected in cases:
        normalized = CodexAdapter._normalize_event(raw)
        assert len(normalized) == 1
        event = normalized[0]
        assert (event["kind"], event["phase"]) == expected[:2]
        assert expected[2] in event["text"] or expected[2] in event["title"]


def test_codex_streams_before_process_completion_and_collects_usage(monkeypatch):
    monkeypatch.setattr("harn.adapters.base.resolve_binary", lambda _b: "/usr/bin/codex")
    emitted = []
    process_finished = {"value": False}
    first_seen = threading.Event()

    lines = [
        json.dumps({"type": "item.completed", "item": {
            "id": "m1", "type": "agent_message", "text": "Live message"}}) + "\n",
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 12, "cached_input_tokens": 3,
            "output_tokens": 4, "reasoning_output_tokens": 2}}) + "\n",
    ]

    class SlowStdout:
        def __iter__(self):
            yield lines[0]
            assert first_seen.wait(1)
            time.sleep(0.01)
            yield lines[1]

    class FakeProcess:
        stdout = SlowStdout()
        stderr = None
        returncode = 0

        def wait(self, timeout=None):
            process_finished["value"] = True
            return 0

        def kill(self):
            self.returncode = -9

    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return FakeProcess()

    monkeypatch.setattr("harn.adapters.codex.subprocess.Popen", fake_popen)
    def capture(event):
        emitted.append(event)
        first_seen.set()

    result = CodexAdapter().run_turn(
        "PROMPT", Path("/tmp/project"), on_event=capture)

    assert "--json" in captured["argv"]
    assert result.ok is True
    assert result.text == "Live message"
    assert result.input_tokens == 12
    assert result.cache_read_tokens == 3
    assert result.output_tokens == 4
    assert emitted[0]["kind"] == "message"
    assert process_finished["value"] is True
