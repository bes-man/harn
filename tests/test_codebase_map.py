"""Service registry: per-service files, index, prompt injection, protocol."""
from __future__ import annotations

from pathlib import Path

from harn import codebase, ENV_DIRNAME


def _env(tmp_path) -> Path:
    env = tmp_path / ENV_DIRNAME
    env.mkdir(parents=True)
    return env


def test_empty_registry(tmp_path):
    env = _env(tmp_path)
    assert codebase.list_services(env) == []
    assert "no services registered" in codebase.index(env)


def test_save_and_read_service(tmp_path):
    env = _env(tmp_path)
    p = codebase.save(env, "Auth API", "owns login, tokens, sessions",
                      "## Responsibility\n- auth\n\n## Standards\n- JWT 15m")
    assert p == env / "services" / "auth-api.md"
    body = codebase.read(env, "auth-api")
    assert "JWT 15m" in body
    # read by unslugged name too
    assert codebase.read(env, "Auth API") == body


def test_index_lists_responsibilities(tmp_path):
    env = _env(tmp_path)
    codebase.save(env, "frontend", "React PWA, all UI", "## Responsibility\n- UI")
    codebase.save(env, "billing", "payments and invoices", "## Responsibility\n- money")
    idx = codebase.index(env)
    assert "frontend: React PWA, all UI" in idx
    assert "billing: payments and invoices" in idx


def test_prompt_note_empty_instructs_seeding(tmp_path):
    note = codebase.prompt_note(_env(tmp_path))
    assert "EMPTY" in note
    assert "save_service" in note


def test_prompt_note_lists_index_and_read_instruction(tmp_path):
    env = _env(tmp_path)
    codebase.save(env, "api", "REST backend", "## Responsibility\n- API")
    note = codebase.prompt_note(env)
    assert "api: REST backend" in note
    assert "read_service" in note


def test_legacy_codebase_md_mentioned(tmp_path):
    env = _env(tmp_path)
    (env / "CODEBASE.md").write_text("# old map")
    note = codebase.prompt_note(env)
    assert "legacy" in note.lower()


def test_save_replaces_existing(tmp_path):
    env = _env(tmp_path)
    codebase.save(env, "api", "v1", "old body")
    codebase.save(env, "api", "v2 responsibility", "new body")
    assert codebase.read(env, "api") is not None
    assert "new body" in codebase.read(env, "api")
    assert ("api", "v2 responsibility") in codebase.list_services(env)


def test_pretask_protocol_in_lean_agents_template():
    from harn.scaffold import _agents_template_text
    text = _agents_template_text("lean")
    for step in ("AS IS", "TO BE", "Skills", "Best practices", "Clarify"):
        assert step in text
    assert "ensure_skill" in text
    assert "ask_user" in text
    assert "context7" in text
    assert "list_services" in text


def test_read_skill_accepts_param_aliases(tmp_path, monkeypatch):
    """read_skill must tolerate name / skill / skill_name (LLMs guess all three)
    — repro of the pydantic 'name field required' error on skill_name=…"""
    import asyncio
    import harn.mcp_server as ms
    env = _env(tmp_path)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    (env / "skills" / "ui").mkdir(parents=True)
    (env / "skills" / "ui" / "SKILL.md").write_text(
        "---\nname: ui\ndescription: UI\n---\n\n# ui\n\nUSE_TOKENS\n")
    srv = ms.build_server()

    async def call(args):
        return await srv.call_tool("read_skill", args)

    for kw in ("name", "skill", "skill_name"):
        result = asyncio.new_event_loop().run_until_complete(call({kw: "ui"}))
        text = str(result)
        assert "USE_TOKENS" in text, f"alias {kw} failed: {text[:120]}"
