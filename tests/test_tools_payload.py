"""Studio Tools tab: custom tools surfaced alongside built-ins, plus
Save/Delete routes with name-uniqueness enforcement against both the
built-in MCP tool names and existing custom tools (Phase 5, Task 3)."""
from __future__ import annotations

from pathlib import Path

from harn import mcp_server, studio, ENV_DIRNAME
from harn import tools as tools_mod


def _env(tmp_path) -> tuple[Path, Path]:
    project_root = tmp_path
    env = project_root / ENV_DIRNAME
    env.mkdir(parents=True, exist_ok=True)
    return env, project_root


def test_tools_catalog_payload_includes_custom_tools(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "Run project lint", ["target"], "npm run lint -- {target}")
    payload = studio.tools_catalog_payload(env)
    custom = payload["custom"]
    assert len(custom) == 1
    assert custom[0]["name"] == "run_lint"
    assert custom[0]["params"] == ["target"]


def test_tools_catalog_payload_still_includes_builtin_tools(tmp_path):
    env, project_root = _env(tmp_path)
    payload = studio.tools_catalog_payload(env)
    assert "list_skills" in payload["tools"]
    assert payload["custom"] == []


def test_save_custom_tool_payload_rejects_name_collision_with_builtin(tmp_path):
    env, project_root = _env(tmp_path)
    builtin_name = next(iter(mcp_server.tool_catalog().keys()))
    result = studio.save_custom_tool_payload(env, {
        "name": builtin_name, "description": "x", "params": [], "command": "echo hi",
    })
    assert result["ok"] is False
    assert builtin_name in result["error"]
    assert tools_mod.read(env, builtin_name) is None


def test_save_custom_tool_payload_rejects_duplicate_custom_name(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "desc", [], "echo hi")
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "y", "params": [], "command": "echo bye",
    })
    assert result["ok"] is False
    assert "run_lint" in result["error"]


def test_save_custom_tool_payload_rejects_invalid_name(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.save_custom_tool_payload(env, {
        "name": "Not A Valid Name!", "description": "x", "params": [], "command": "echo hi",
    })
    assert result["ok"] is False


def test_save_custom_tool_payload_succeeds_for_a_new_name(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "Run lint", "params": ["target"],
        "command": "npm run lint -- {target}", "source": "upload",
    })
    assert result["ok"] is True
    assert tools_mod.read(env, "run_lint") is not None


def test_delete_custom_tool_payload_removes_it(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "desc", [], "echo hi")
    result = studio.delete_custom_tool_payload(env, "run_lint")
    assert result["ok"] is True
    assert tools_mod.read(env, "run_lint") is None


def test_delete_custom_tool_payload_reports_unknown_name(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.delete_custom_tool_payload(env, "nope")
    assert result["ok"] is False


def test_save_custom_tool_payload_writes_the_uploaded_script(tmp_path):
    import base64
    env, project_root = _env(tmp_path)
    content_b64 = base64.b64encode(b"echo hello\n").decode()
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "d", "params": [],
        "command": "bash lint.sh", "source": "upload",
        "script_name": "lint.sh", "content_b64": content_b64,
    })
    assert result["ok"] is True
    script_path = env / "tools" / "lint.sh"
    assert script_path.read_bytes() == b"echo hello\n"


def test_save_custom_tool_payload_rejects_path_traversal_in_script_name(tmp_path):
    import base64
    env, project_root = _env(tmp_path)
    content_b64 = base64.b64encode(b"evil\n").decode()
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "d", "params": [],
        "command": "bash lint.sh", "source": "upload",
        "script_name": "../../evil.sh", "content_b64": content_b64,
    })
    # script_name is sanitized to its basename, so this must NOT escape the
    # tools dir -- it either lands safely inside it or is rejected outright.
    assert not (project_root / "evil.sh").exists()
    assert not (project_root.parent / "evil.sh").exists()


def test_draft_tool_chat_payload_calls_one_agent_turn_and_parses_a_draft(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)

    class FakeAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            class R:
                ok = True
                text = (
                    "Sure, here's a tool that runs your linter:\n\n"
                    "```json\n"
                    '{"name": "run_lint", "description": "Run project lint", '
                    '"params": ["target"], "command": "npm run lint -- {target}"}\n'
                    "```\n"
                )
            return R()

    monkeypatch.setattr(studio, "_pick_adapter", lambda cfg: FakeAdapter())
    from harn.config import Config
    result = studio.draft_tool_chat_payload(env, project_root, Config(), {
        "history": [], "message": "I want a tool that runs my linter",
    })
    assert "here's a tool" in result["reply"]
    assert result["draft"]["name"] == "run_lint"
    assert result["draft"]["params"] == ["target"]


def test_draft_tool_chat_payload_keeps_no_draft_when_reply_has_none(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)

    class FakeAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            class R:
                ok = True
                text = "Can you tell me more about what the tool should do?"
            return R()

    monkeypatch.setattr(studio, "_pick_adapter", lambda cfg: FakeAdapter())
    from harn.config import Config
    result = studio.draft_tool_chat_payload(env, project_root, Config(), {
        "history": [], "message": "make me a tool",
    })
    assert result["draft"] is None


def test_draft_tool_chat_payload_includes_full_transcript_in_the_prompt(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)
    captured = {}

    class FakeAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            captured["prompt"] = prompt
            class R:
                ok = True
                text = "ok"
            return R()

    monkeypatch.setattr(studio, "_pick_adapter", lambda cfg: FakeAdapter())
    from harn.config import Config
    studio.draft_tool_chat_payload(env, project_root, Config(), {
        "history": [{"role": "user", "text": "first message"},
                    {"role": "agent", "text": "first reply"}],
        "message": "second message",
    })
    assert "first message" in captured["prompt"]
    assert "first reply" in captured["prompt"]
    assert "second message" in captured["prompt"]


def test_import_custom_tool_bundle_payload_imports_a_plain_json_export(tmp_path):
    import base64
    src_env, _ = _env(tmp_path / "src")
    dst_env, _ = _env(tmp_path / "dst")
    tools_mod.save(src_env, "echo_it", "desc", ["msg"], "echo {msg}", source="chat")
    data = tools_mod.export_bundle(tools_mod.read(src_env, "echo_it"))
    result = studio.import_custom_tool_bundle_payload(dst_env, {
        "filename": "echo_it.json", "content_b64": base64.b64encode(data).decode(),
    })
    assert result["ok"] is True
    assert result["name"] == "echo_it"
    assert tools_mod.read(dst_env, "echo_it").command == "echo {msg}"


def test_import_custom_tool_bundle_payload_imports_a_zip_export_with_its_script(tmp_path):
    import base64
    src_env, _ = _env(tmp_path / "src")
    dst_env, _ = _env(tmp_path / "dst")
    p = tools_mod.save(src_env, "run_lint", "desc", [], "bash lint.sh", source="upload")
    (p.parent / "lint.sh").write_text("echo hi\n", encoding="utf-8")
    data = tools_mod.export_bundle(tools_mod.read(src_env, "run_lint"))
    result = studio.import_custom_tool_bundle_payload(dst_env, {
        "filename": "run_lint.zip", "content_b64": base64.b64encode(data).decode(),
    })
    assert result["ok"] is True
    assert (dst_env / "tools" / "lint.sh").read_text() == "echo hi\n"


def test_import_custom_tool_bundle_payload_rejects_a_name_collision(tmp_path):
    import base64
    env, _ = _env(tmp_path / "dst")
    other_env, _ = _env(tmp_path / "src")
    tools_mod.save(env, "echo_it", "existing", ["msg"], "echo {msg}", source="chat")
    tools_mod.save(other_env, "echo_it", "different", [], "echo hi", source="chat")
    data = tools_mod.export_bundle(tools_mod.read(other_env, "echo_it"))
    result = studio.import_custom_tool_bundle_payload(env, {
        "filename": "echo_it.json", "content_b64": base64.b64encode(data).decode(),
    })
    assert result["ok"] is False
    assert "already exists" in result["error"]


def test_import_custom_tool_bundle_payload_rejects_a_builtin_name_collision(tmp_path):
    import base64
    import json as json_mod
    env, _ = _env(tmp_path / "dst")
    built_in = next(iter(mcp_server.tool_catalog().keys()))
    data = json_mod.dumps({
        "name": built_in, "description": "d", "params": [], "command": "echo hi",
        "source": "chat",
    }).encode("utf-8")
    result = studio.import_custom_tool_bundle_payload(env, {
        "filename": f"{built_in}.json", "content_b64": base64.b64encode(data).decode(),
    })
    assert result["ok"] is False
    assert "built-in" in result["error"]
    assert tools_mod.read(env, built_in) is None


def test_import_custom_tool_bundle_payload_rejects_bad_base64(tmp_path):
    env, _ = _env(tmp_path / "dst")
    result = studio.import_custom_tool_bundle_payload(env, {
        "filename": "x.json", "content_b64": "not-valid-base64!!!",
    })
    assert result["ok"] is False


def test_import_custom_tool_bundle_payload_rejects_a_zip_with_path_traversal_script_name(tmp_path):
    """Mirrors test_save_custom_tool_payload_rejects_path_traversal_in_script_name
    (Task 4) for the import route: a malicious bundle's embedded script
    entry name is a traversal payload, and the import route must not let it
    escape the tools directory."""
    import base64
    import io
    import zipfile
    import json as json_mod
    env, project_root = _env(tmp_path / "dst")
    tool_json = json_mod.dumps({
        "name": "run_lint", "description": "d", "params": [],
        "command": "bash lint.sh", "source": "upload",
    }).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("run_lint.json", tool_json)
        zf.writestr("../../evil.sh", "evil\n")
    result = studio.import_custom_tool_bundle_payload(env, {
        "filename": "run_lint.zip", "content_b64": base64.b64encode(buf.getvalue()).decode(),
    })
    assert result["ok"] is True
    assert not (tmp_path / "evil.sh").exists()
    assert not (project_root.parent / "evil.sh").exists()
