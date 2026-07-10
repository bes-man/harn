from pathlib import Path

import pytest

from harn import state, tasks, skills, scaffold, ENV_DIRNAME


def test_state_block_and_answer(tmp_path: Path):
    st = state.State()
    assert st.phase == state.PLANNING
    st.block("which database?")
    assert st.phase == state.BLOCKED
    assert st.question == "which database?"
    assert st.blocked_since is not None
    st.answer("postgres")
    assert st.phase == state.READY
    assert st.last_answer == "postgres"
    assert st.question is None


def test_state_invalid_phase():
    with pytest.raises(ValueError):
        state.State().transition("NONSENSE")


def test_state_roundtrip(tmp_path: Path):
    st = state.State(phase=state.EXECUTING, current_task="t1", iterations=3)
    st.save(tmp_path)
    loaded = state.State.load(tmp_path)
    assert loaded.phase == state.EXECUTING
    assert loaded.current_task == "t1"
    assert loaded.iterations == 3


def test_current_step_round_trips_through_save_load(tmp_path: Path):
    st = state.State(current_task="t1", current_step="s2")
    st.save(tmp_path)
    reloaded = state.State.load(tmp_path)
    assert reloaded.current_step == "s2"


def test_current_step_defaults_to_none(tmp_path: Path):
    st = state.State(current_task="t1")
    st.save(tmp_path)
    reloaded = state.State.load(tmp_path)
    assert reloaded.current_step is None


def test_next_task_priority(tmp_path: Path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    from .conftest import make_task
    make_task(env, "PRJ-low", title="Low", priority=90)
    make_task(env, "PRJ-high", title="High", priority=5)
    nxt = tasks.next_task(env)
    assert nxt is not None and nxt.id == "PRJ-high"


def test_mark_done_skips_completed(tmp_path: Path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    from .conftest import make_task
    t = make_task(env, "PRJ-001", title="Something")
    tasks.mark_done(t)
    reparsed = tasks.find(env, "PRJ-001")
    assert reparsed.done is True


def test_skill_index_and_body(tmp_path: Path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    idx = skills.index(env)
    assert "security:" in idx
    body = skills.read_skill(env, "security")
    assert body and "secret" in body.lower()
    # frontmatter is stripped from the body
    assert not body.startswith("---")
