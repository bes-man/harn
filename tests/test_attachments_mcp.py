"""MCP tools for task attachments: save_attachment/list_attachments/
read_attachment — the agent-facing side of harn/attachments.py."""
from __future__ import annotations

import base64
from pathlib import Path

from harn import tasks, attachments, ENV_DIRNAME


def _env(tmp_path: Path) -> Path:
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    return env


def _tool_fn(env: Path, name: str, monkeypatch):
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    from harn import mcp_server as ms
    server = ms.build_server()
    return next(t.fn for t in server._tool_manager._tools.values() if t.name == name)


def test_save_attachment_writes_file(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "save_attachment", monkeypatch)
    b64 = base64.b64encode(b"fake png bytes").decode()
    result = fn(task_id=t.id, filename="ref.png", content_b64=b64)

    assert "Saved as ref.png" in result
    assert attachments.read_bytes(env, t.id, "ref.png") == b"fake png bytes"


def test_save_attachment_unknown_task(tmp_path, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    fn = _tool_fn(env, "save_attachment", monkeypatch)
    result = fn(task_id="NOPE", filename="a.png", content_b64=base64.b64encode(b"x").decode())
    assert "not found" in result


def test_save_attachment_rejects_bad_base64(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    fn = _tool_fn(env, "save_attachment", monkeypatch)
    result = fn(task_id=t.id, filename="a.png", content_b64="not-valid-base64!!!")
    assert "not valid base64" in result
    assert attachments.list_files(env, t.id) == []


def test_list_attachments_empty(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    fn = _tool_fn(env, "list_attachments", monkeypatch)
    assert "no attachments" in fn(task_id=t.id)


def test_list_attachments_shows_saved_files(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    attachments.save(env, t.id, "ref.png", b"x")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    fn = _tool_fn(env, "list_attachments", monkeypatch)
    out = fn(task_id=t.id)
    assert "ref.png" in out and "image" in out


def test_read_attachment_image_returns_image_content(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    # a minimal valid-enough PNG signature so Image(path=...) can read bytes
    attachments.save(env, t.id, "ref.png", b"\x89PNG\r\n\x1a\nrest")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    fn = _tool_fn(env, "read_attachment", monkeypatch)
    result = fn(task_id=t.id, filename="ref.png")

    from mcp.server.fastmcp import Image
    assert isinstance(result, Image)


def test_read_attachment_text_file_returns_text(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    attachments.save(env, t.id, "notes.txt", "hello world".encode())
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    fn = _tool_fn(env, "read_attachment", monkeypatch)
    result = fn(task_id=t.id, filename="notes.txt")
    assert result == "hello world"


def test_read_attachment_missing_file(tmp_path, monkeypatch):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    fn = _tool_fn(env, "read_attachment", monkeypatch)
    result = fn(task_id=t.id, filename="nope.png")
    assert "no attachment" in result
