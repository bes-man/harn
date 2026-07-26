"""MCP answer_question tool: clears block, saves to ANSWERS.md, promotes skill."""
from __future__ import annotations

from pathlib import Path

from harn import loop, state, skills, ENV_DIRNAME
from harn.config import Config


def _mcp_tool_fn(env, name, monkeypatch):
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)
    from harn import mcp_server as ms
    server = ms.build_server()
    return next(t.fn for t in server._tool_manager._tools.values() if t.name == name)


def _make_env(tmp_path: Path) -> Path:
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    (env / "skills").mkdir(parents=True)
    return env


def test_answer_question_clears_block(tmp_path):
    env = _make_env(tmp_path)
    state_dir = env / "state"
    state.blocked_marker(state_dir).write_text("Which DB?", encoding="utf-8")
    st = state.State.load(state_dir)
    st.block("Which DB?")
    st.save(state_dir)

    loop.answer(env, "Use PostgreSQL")

    assert state.read_block_question(state_dir) is None
    st2 = state.State.load(state_dir)
    assert st2.last_answer == "Use PostgreSQL"


def test_answer_question_writes_answers_md(tmp_path):
    env = _make_env(tmp_path)
    state_dir = env / "state"
    state.blocked_marker(state_dir).write_text("Token TTL?", encoding="utf-8")
    st = state.State.load(state_dir)
    st.block("Token TTL?")
    st.save(state_dir)

    loop.answer(env, "15 minutes")

    answers = (state_dir / "ANSWERS.md").read_text(encoding="utf-8")
    assert "Token TTL?" in answers
    assert "15 minutes" in answers


def test_answer_question_promotes_to_skill(tmp_path):
    env = _make_env(tmp_path)
    state_dir = env / "state"
    state.blocked_marker(state_dir).write_text("Auth approach?", encoding="utf-8")
    state.set_block_skill(state_dir, "security")
    st = state.State.load(state_dir)
    st.block("Auth approach?")
    st.save(state_dir)

    loop.answer(env, "JWT, 15m expiry")

    skill_file = env / "skills" / "security" / "SKILL.md"
    assert skill_file.exists()
    content = skill_file.read_text(encoding="utf-8")
    assert "JWT" in content


def test_answer_question_no_pending_is_noop(tmp_path):
    env = _make_env(tmp_path)
    # No block present — answer() should not crash
    loop.answer(env, "surprise answer")
    # ANSWERS.md may or may not exist but state is consistent
    assert state.read_block_question(env / "state") is None


# --- Telegram source + check_pending_answer ---

def _block(env: Path, question: str) -> None:
    state_dir = env / "state"
    state.blocked_marker(state_dir).write_text(question, encoding="utf-8")
    st = state.State.load(state_dir)
    st.block(question)
    st.save(state_dir)


def test_telegram_source_writes_pending_file(tmp_path):
    env = _make_env(tmp_path)
    _block(env, "Use Redis or Memcached?")
    loop.answer(env, "Redis", source="telegram")
    pending = env / "state" / "PENDING_TELEGRAM_ANSWER.txt"
    assert pending.exists()
    assert "Redis" in pending.read_text(encoding="utf-8")


def test_chat_source_does_not_write_pending_file(tmp_path):
    env = _make_env(tmp_path)
    _block(env, "Use Redis or Memcached?")
    loop.answer(env, "Redis")  # default source="li"
    assert not (env / "state" / "PENDING_TELEGRAM_ANSWER.txt").exists()


def test_auto_source_writes_pending_file(tmp_path):
    env = _make_env(tmp_path)
    _block(env, "Decide for me?")
    loop.answer(env, "Agent chose Redis", source="auto")
    assert (env / "state" / "PENDING_TELEGRAM_ANSWER.txt").exists()


def _tool_fn(env: Path, name: str, monkeypatch):
    import os
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    from harn import mcp_server as ms
    server = ms.build_server()
    return next(t.fn for t in server._tool_manager._tools.values() if t.name == name)


def test_check_pending_answer_returns_and_clears(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    pending = env / "state" / "PENDING_TELEGRAM_ANSWER.txt"
    pending.write_text("Use Redis", encoding="utf-8")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "check_pending_answer", monkeypatch)
    result = fn()
    assert "Redis" in result
    assert not pending.exists()


def test_check_pending_answer_still_waiting(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    _block(env, "Which cache?")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "check_pending_answer", monkeypatch)
    result = fn()
    assert "still_waiting" in result


def test_check_pending_answer_no_pending(tmp_path, monkeypatch):
    env = _make_env(tmp_path)
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "check_pending_answer", monkeypatch)
    result = fn()
    assert result == "no_pending_answer"


def test_studio_source_writes_pending_file(tmp_path):
    """An answer typed in Studio reaches a stopped agent exactly the way a
    Telegram one does — the agent process can't see the web UI either."""
    env = _make_env(tmp_path)
    _block(env, "Use Redis or Memcached?")
    loop.answer(env, "Redis", source="studio")
    pending = env / "state" / "PENDING_TELEGRAM_ANSWER.txt"
    assert pending.exists() and "Redis" in pending.read_text(encoding="utf-8")


def test_a_newer_answer_supersedes_a_stale_pending_one(tmp_path):
    """Observed live: the file is consumed once but was only ever WRITTEN by
    two sources, so a leftover "You pressed 'Decide for me'" sat on disk and
    was served as the reply to a LATER answer typed in the UI."""
    env = _make_env(tmp_path)
    _block(env, "Which mechanism?")
    loop.answer(env, "You pressed 'Decide for me'.", source="auto")
    _block(env, "Which mechanism?")
    loop.answer(env, "Use in-app credit", source="studio")

    pending = (env / "state" / "PENDING_TELEGRAM_ANSWER.txt").read_text(encoding="utf-8")
    assert "in-app credit" in pending
    assert "Decide for me" not in pending


def test_a_chat_answer_clears_a_stale_pending_file(tmp_path):
    """Same hazard from the other direction: answering in-session must not
    leave an older out-of-session answer queued up to be served next."""
    env = _make_env(tmp_path)
    _block(env, "Which mechanism?")
    loop.answer(env, "You pressed 'Decide for me'.", source="auto")
    _block(env, "Which mechanism?")
    loop.answer(env, "answered in chat")          # default source="cli"
    assert not (env / "state" / "PENDING_TELEGRAM_ANSWER.txt").exists()


def test_the_pending_answer_names_the_channel_it_came_from(tmp_path, monkeypatch):
    """Hardcoding "via Telegram/auto" is how a human who typed a real answer
    in the UI got told the agent had received "You pressed 'Decide for me'"."""
    env = _make_env(tmp_path)
    _block(env, "Which mechanism?")
    loop.answer(env, "Use in-app credit", source="studio")

    fn = _mcp_tool_fn(env, "check_pending_answer", monkeypatch)
    out = fn()
    assert "via studio" in out and "in-app credit" in out


def test_an_older_plain_text_pending_file_is_still_readable(tmp_path, monkeypatch):
    """A file written by a pre-JSON harn must survive an upgrade."""
    env = _make_env(tmp_path)
    (env / "state").mkdir(parents=True, exist_ok=True)
    (env / "state" / "PENDING_TELEGRAM_ANSWER.txt").write_text(
        "plain old answer", encoding="utf-8")
    fn = _mcp_tool_fn(env, "check_pending_answer", monkeypatch)
    assert "plain old answer" in fn()
