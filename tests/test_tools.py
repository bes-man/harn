"""Tests for harn/tools.py — custom-tool storage and execution."""
import json

from harn import tools


def test_save_creates_a_json_file_and_discover_finds_it(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    p = tools.save(env, "run_lint", "Run project lint", ["target"],
                   "npm run lint -- {target}")
    assert p.exists()
    found = tools.discover(env)
    assert len(found) == 1
    assert found[0].name == "run_lint"
    assert found[0].params == ["target"]


def test_save_rejects_invalid_name(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    try:
        tools.save(env, "Run Lint!", "desc", [], "echo hi")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_save_rejects_duplicate_custom_tool_name(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "run_lint", "desc", [], "echo hi")
    try:
        tools.save(env, "run_lint", "other desc", [], "echo bye")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_name_taken(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    assert tools.name_taken(env, "run_lint") is False
    tools.save(env, "run_lint", "desc", [], "echo hi")
    assert tools.name_taken(env, "run_lint") is True


def test_read_returns_none_for_missing(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    assert tools.read(env, "nope") is None


def test_delete_removes_the_tool(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "run_lint", "desc", [], "echo hi")
    assert tools.delete(env, "run_lint") is True
    assert tools.discover(env) == []
    assert tools.delete(env, "run_lint") is False


def test_index_lists_name_and_description(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "run_lint", "Run project lint", [], "echo hi")
    assert "run_lint: Run project lint" in tools.index(env)


def test_execute_substitutes_params_with_shlex_quote(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tools.save(env, "echo_it", "echo the value", ["msg"], "echo {msg}")
    tool = tools.read(env, "echo_it")
    out = tools.execute(tool, {"msg": "hello world"}, cwd=tmp_path)
    assert out.strip() == "hello world"


def test_execute_quotes_a_param_that_looks_like_a_second_command(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tools.save(env, "echo_it", "echo the value", ["msg"], "echo {msg}")
    tool = tools.read(env, "echo_it")
    out = tools.execute(tool, {"msg": "; rm -rf /tmp/should-not-run"}, cwd=tmp_path)
    # The whole malicious-looking string prints as ONE literal argument to echo —
    # it must never execute as a second shell command.
    assert "; rm -rf /tmp/should-not-run" in out


def test_execute_reports_command_not_found(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "nope_cmd", "desc", [], "this-binary-does-not-exist-xyz")
    tool = tools.read(env, "nope_cmd")
    out = tools.execute(tool, {}, cwd=tmp_path)
    assert "command not found" in out
