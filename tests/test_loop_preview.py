"""Tests for loop.preview_step_prompt / loop.save_context_export (Phase 4)."""
from pathlib import Path

from harn import loop, tasks, workflow
from harn.config import Config


def _make_env(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    return env, project_root


def test_preview_step_prompt_matches_a_real_turns_prompt(tmp_path, monkeypatch):
    env, project_root = _make_env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    step = {"title": "Only step", "id": "s1", "body": "do it well",
            "required": [], "tools": [], "enabled": True}
    cfg = Config()

    captured = {}

    def fake_run_turn(adapter, env_dir, prompt, project_root, **kw):
        captured["prompt"] = prompt
        class R:
            ok = True
            text = "done"
            total_tokens = 10
            def tail(self, n): return "done"
            def usage_str(self): return ""
        return R()

    monkeypatch.setattr(loop, "_run_turn", fake_run_turn)
    preview = loop.preview_step_prompt(env, cfg, task, step)
    # No turn has actually run — no ledger entry should exist yet.
    fresh = tasks.find(env, task.id)
    assert "s1" not in fresh.step_results
    # The preview must be byte-identical to what _build_step_prompt itself produces.
    assert preview == loop._build_step_prompt(env, cfg, task, step)


def test_save_context_export_writes_a_file_and_returns_its_path(tmp_path):
    env, _ = _make_env(tmp_path)
    p = loop.save_context_export(env, "task-1", "s1", "the full prompt text")
    assert p.exists()
    assert p.read_text(encoding="utf-8") == "the full prompt text"
    assert p.parent == env / "state" / "context_exports"
    assert "task-1" in p.name and "s1" in p.name
