"""Auto-promotion: a skill-tagged ask_user answer is saved into that skill."""
from __future__ import annotations

from pathlib import Path

from harn import state, loop, skills


def _blocked(env: Path, question: str, skill: str | None = None):
    sd = env / "state"
    sd.mkdir(parents=True, exist_ok=True)
    state.blocked_marker(sd).write_text(question, encoding="utf-8")
    if skill:
        state.set_block_skill(sd, skill)
    st = state.State.load(sd)
    st.block(question)
    st.save(sd)


def test_answer_promotes_into_tagged_skill(tmp_path):
    env = tmp_path / "harn_env"
    _blocked(env, "Minimum password length?", skill="security")

    loop.answer(env, "At least 12 characters")

    body = skills.read_skill(env, "security")
    assert body and "12 characters" in body
    # the hint is cleared after promotion
    assert state.read_block_skill(env / "state") is None


def test_answer_without_hint_does_not_promote(tmp_path):
    env = tmp_path / "harn_env"
    _blocked(env, "Rename this var?", skill=None)

    loop.answer(env, "yes, call it total")

    # no skill was created/written
    assert skills.read_skill(env, "security") is None


def test_set_and_read_block_skill(tmp_path):
    sd = tmp_path / "state"
    state.set_block_skill(sd, "frontend")
    assert state.read_block_skill(sd) == "frontend"
    state.clear_block_skill(sd)
    assert state.read_block_skill(sd) is None


def test_promotion_logged_to_progress(tmp_path):
    env = tmp_path / "harn_env"
    _blocked(env, "Which test runner?", skill="testing")
    loop.answer(env, "pytest")
    from harn import progress
    log = progress.tail(env)
    assert "promoted answer into skill 'testing'" in log
