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
    # per-agent glue
    assert (tmp_path / ".mcp.json").exists()
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert (env / "mcp_snippets.md").exists()
    assert result["env_dir"] == str(env)


def test_setup_does_not_clobber(tmp_path: Path):
    scaffold.setup(tmp_path)
    custom = tmp_path / ENV_DIRNAME / "skills" / "project" / "SKILL.md"
    custom.write_text("CUSTOM")
    scaffold.setup(tmp_path)  # run again
    assert custom.read_text() == "CUSTOM"  # fork/local edits preserved
