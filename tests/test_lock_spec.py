"""`tasks.lock_spec` data model — the locked-spec flag + criteria persist.

Split out of the deleted test_funnel.py (Task 4 removed the planning funnel from
the loop, but `lock_spec`/`spec_locked` remain live task-model behaviour a task
can still use)."""
from __future__ import annotations

from pathlib import Path

from harn import scaffold, tasks, ENV_DIRNAME


def _proj(tmp_path) -> Path:
    scaffold.setup(tmp_path)
    return tmp_path / ENV_DIRNAME


def test_lock_spec_sets_flag_and_criteria(tmp_path):
    env = _proj(tmp_path)
    t = tasks.create_task(env, "Add login", task_id="PRJ-001",
                          description="## What\nx\n## Done when\n- TBD")
    assert t.spec_locked is False
    tasks.lock_spec(t, "- POST /login returns JWT\n- 401 on bad creds",
                    approach="bcrypt + jose",
                    decisions=[("15m token TTL", "security")])
    t2 = tasks.find(env, "PRJ-001")
    assert t2.spec_locked is True
    assert "POST /login returns JWT" in t2.description
    assert "Approach (locked)" in t2.description
    assert any("15m token TTL" in d.decision for d in t2.decisions)


def test_spec_locked_round_trips(tmp_path):
    env = _proj(tmp_path)
    t = tasks.create_task(env, "x", task_id="P1")
    tasks.lock_spec(t, "- done")
    assert tasks.find(env, "P1").spec_locked is True
