"""Per-task file attachments (harn/attachments.py) — images/files a human or
agent saves against a task, discovered by listing the directory (no task-JSON
field to keep in sync, same pattern as design.py)."""
from __future__ import annotations

from harn import attachments, ENV_DIRNAME


def _env(tmp_path):
    return tmp_path / ENV_DIRNAME


def test_save_and_list(tmp_path):
    env = _env(tmp_path)
    attachments.save(env, "PRJ-001", "design.png", b"\x89PNG fake bytes")
    rows = attachments.list_files(env, "PRJ-001")
    assert len(rows) == 1
    assert rows[0]["name"] == "design.png"
    assert rows[0]["kind"] == "image"
    assert rows[0]["size"] == len(b"\x89PNG fake bytes")


def test_non_image_kind(tmp_path):
    env = _env(tmp_path)
    attachments.save(env, "PRJ-001", "notes.txt", b"hello")
    rows = attachments.list_files(env, "PRJ-001")
    assert rows[0]["kind"] == "file"


def test_list_empty_for_untouched_task(tmp_path):
    env = _env(tmp_path)
    assert attachments.list_files(env, "PRJ-999") == []


def test_read_bytes_round_trips(tmp_path):
    env = _env(tmp_path)
    attachments.save(env, "PRJ-001", "a.png", b"content-here")
    assert attachments.read_bytes(env, "PRJ-001", "a.png") == b"content-here"


def test_read_bytes_missing_file(tmp_path):
    env = _env(tmp_path)
    assert attachments.read_bytes(env, "PRJ-001", "nope.png") is None


def test_duplicate_filename_never_overwrites(tmp_path):
    env = _env(tmp_path)
    attachments.save(env, "PRJ-001", "design.png", b"v1")
    attachments.save(env, "PRJ-001", "design.png", b"v2")
    rows = {r["name"]: r for r in attachments.list_files(env, "PRJ-001")}
    assert "design.png" in rows and "design (1).png" in rows
    assert attachments.read_bytes(env, "PRJ-001", "design.png") == b"v1"
    assert attachments.read_bytes(env, "PRJ-001", "design (1).png") == b"v2"


def test_delete_removes_file(tmp_path):
    env = _env(tmp_path)
    attachments.save(env, "PRJ-001", "a.png", b"x")
    assert attachments.delete(env, "PRJ-001", "a.png") is True
    assert attachments.list_files(env, "PRJ-001") == []


def test_delete_missing_file_returns_false(tmp_path):
    env = _env(tmp_path)
    assert attachments.delete(env, "PRJ-001", "nope.png") is False


def test_path_traversal_blocked_on_save(tmp_path):
    env = _env(tmp_path)
    p = attachments.save(env, "PRJ-001", "../../evil.txt", b"x")
    # basename-only: lands inside the task's own storage dir, not escaped
    assert p.parent == attachments.attachments_dir(env, "PRJ-001")
    assert p.name == "evil.txt"


def test_path_traversal_blocked_on_read(tmp_path):
    env = _env(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    assert attachments.read_bytes(env, "PRJ-001", "../../secret.txt") is None


def test_is_image_extensions(tmp_path):
    assert attachments.is_image("x.png") and attachments.is_image("X.JPG")
    assert not attachments.is_image("x.pdf") and not attachments.is_image("x.txt")


def test_tasks_are_isolated(tmp_path):
    env = _env(tmp_path)
    attachments.save(env, "PRJ-001", "a.png", b"1")
    attachments.save(env, "PRJ-002", "b.png", b"2")
    assert [r["name"] for r in attachments.list_files(env, "PRJ-001")] == ["a.png"]
    assert [r["name"] for r in attachments.list_files(env, "PRJ-002")] == ["b.png"]
