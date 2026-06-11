from pathlib import Path

from harn import scaffold, ENV_DIRNAME


def test_setup_creates_env(tmp_path: Path):
    result = scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME

    assert env.is_dir()
    assert (env / "harn.toml").exists()
    assert (env / "state").is_dir()
    # AGENTS.md is moved to the project root (portable instruction file)
    assert (tmp_path / "AGENTS.md").exists()
    assert not (env / "AGENTS.md").exists()
    # all six default skills present
    for name in ["project", "standards", "architecture", "security", "ui", "constraints"]:
        assert (env / "skills" / name / "SKILL.md").exists()
    # default agent is claude → only .mcp.json in root, NOT .cursor/mcp.json
    assert (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".cursor" / "mcp.json").exists()
    # root connectors are git-ignored (clean project history)
    gi = (tmp_path / ".gitignore").read_text()
    assert ".mcp.json" in gi and "AGENTS.md" in gi
    assert result["env_dir"] == str(env)


def test_setup_cursor_agent_writes_cursor_config(tmp_path: Path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text('[harn]\nagent = "cursor"\n')
    scaffold.setup(tmp_path)
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert not (tmp_path / ".mcp.json").exists()   # claude not in chain


def test_teardown_removes_root_connectors(tmp_path: Path):
    scaffold.setup(tmp_path)
    assert (tmp_path / ".mcp.json").exists()
    removed = scaffold.teardown(tmp_path)
    assert ".mcp.json" in removed and "AGENTS.md" in removed
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    # harn_env stays
    assert (tmp_path / ENV_DIRNAME / "harn.toml").exists()


def test_setup_does_not_clobber(tmp_path: Path):
    scaffold.setup(tmp_path)
    custom = tmp_path / ENV_DIRNAME / "skills" / "project" / "SKILL.md"
    custom.write_text("CUSTOM")
    scaffold.setup(tmp_path)  # run again
    assert custom.read_text() == "CUSTOM"  # fork/local edits preserved


def test_setup_creates_claude_md_importing_agents(tmp_path):
    from harn import scaffold
    (tmp_path / "pkg.json").write_text("{}")  # minimal project marker
    scaffold.setup(tmp_path)
    claude = tmp_path / "CLAUDE.md"
    assert claude.exists()
    text = claude.read_text()
    assert "@AGENTS.md" in text                     # imports the protocol
    assert "AskUserQuestion" in text                # front-loads the key rule
    assert (tmp_path / "AGENTS.md").exists()


def test_refresh_agents_md_updates_and_backs_up(tmp_path):
    from harn import scaffold
    (tmp_path / "AGENTS.md").write_text("OLD STALE CONTENT")
    written = scaffold.refresh_agents_md(tmp_path)
    assert written is not None
    assert (tmp_path / "AGENTS.md.bak").read_text() == "OLD STALE CONTENT"
    assert "OLD STALE CONTENT" not in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / "CLAUDE.md").exists()


def test_refresh_preserves_user_claude_md(tmp_path):
    from harn import scaffold
    (tmp_path / "CLAUDE.md").write_text("# My own rules\nDo X.\n")
    scaffold.refresh_agents_md(tmp_path)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "My own rules" in text          # user content kept
    assert "@AGENTS.md" in text            # import appended


def test_context7_in_mcp_servers_by_default(tmp_path):
    from harn import scaffold
    scaffold.setup(tmp_path)
    import json
    cfg = json.loads((tmp_path / ".mcp.json").read_text())
    assert "context7" in cfg["mcpServers"]


def test_context7_disabled_via_config(tmp_path):
    from harn import scaffold
    scaffold.setup(tmp_path)
    toml = tmp_path / "harn_env" / "harn.toml"
    toml.write_text(toml.read_text().replace("context7 = true", "context7 = false"))
    scaffold.setup(tmp_path)  # regenerate configs
    import json
    cfg = json.loads((tmp_path / ".mcp.json").read_text())
    assert "context7" not in cfg["mcpServers"]
