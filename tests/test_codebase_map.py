"""Codebase map: storage, prompt injection, protocol presence."""
from __future__ import annotations

from pathlib import Path

from harn import codebase, ENV_DIRNAME


def _env(tmp_path) -> Path:
    env = tmp_path / ENV_DIRNAME
    env.mkdir(parents=True)
    return env


def test_read_missing_returns_none(tmp_path):
    assert codebase.read(_env(tmp_path)) is None


def test_save_and_read_roundtrip(tmp_path):
    env = _env(tmp_path)
    p = codebase.save(env, "# Codebase map\n\n## Stack\n- Python")
    assert p == env / "CODEBASE.md"
    assert "Python" in codebase.read(env)


def test_prompt_note_missing_instructs_creation(tmp_path):
    note = codebase.prompt_note(_env(tmp_path))
    assert "MISSING" in note
    assert "update_codebase_map" in note


def test_prompt_note_includes_map(tmp_path):
    env = _env(tmp_path)
    codebase.save(env, "# Map\n- service A: handles auth")
    note = codebase.prompt_note(env)
    assert "service A" in note
    assert "AS IS" in note


def test_prompt_note_truncates_long_map(tmp_path):
    env = _env(tmp_path)
    codebase.save(env, "x" * 10_000)
    note = codebase.prompt_note(env, limit=1000)
    assert "truncated" in note
    assert len(note) < 2000


def test_pretask_protocol_in_mcp_module():
    from harn.mcp_server import _PRETASK_PROTOCOL
    for step in ("AS IS", "TO BE", "Skills", "Best practices", "Clarify"):
        assert step in _PRETASK_PROTOCOL
    assert "ensure_skill" in _PRETASK_PROTOCOL
    assert "ask_user" in _PRETASK_PROTOCOL
    assert "context7" in _PRETASK_PROTOCOL
