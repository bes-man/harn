"""MCP answer_question tool: clears block, saves to ANSWERS.md, promotes skill."""
from __future__ import annotations

from pathlib import Path

from harn import loop, state, skills, ENV_DIRNAME
from harn.config import Config


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
