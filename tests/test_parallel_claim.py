"""Dependency-aware eligibility + atomic task claiming for parallel work."""
from __future__ import annotations

import concurrent.futures as cf
from pathlib import Path

from harn import tasks, ENV_DIRNAME
from tests.conftest import make_task


def _env(tmp_path) -> Path:
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    return env


def test_dep_blocks_until_done(tmp_path):
    env = _env(tmp_path)
    a = tasks.create_task(env, "API", task_id="A", priority=1)
    tasks.create_task(env, "Wire UI", task_id="B", priority=1, depends_on=["A"])
    # B waits on A → only A is runnable
    first = tasks.next_task(env)
    assert first.id == "A"
    # finish A → B becomes runnable
    tasks.accept(tasks.find(env, "A"), by="me")
    tasks.mark_done(tasks.find(env, "A"))
    assert tasks.next_task(env).id == "B"


def test_only_restricts_pool_to_one_task(tmp_path):
    """`only` (harn run --task, the studio Launch button) ignores priority
    ordering and works exactly the named task, even if a higher-priority one
    is also runnable."""
    env = _env(tmp_path)
    tasks.create_task(env, "High prio", task_id="A", priority=1)
    tasks.create_task(env, "Low prio", task_id="B", priority=99)
    assert tasks.next_task(env).id == "A"          # normal pick: highest priority
    assert tasks.next_task(env, only="B").id == "B"  # restricted: exactly B
    assert tasks.next_task(env, only="nope") is None  # unknown id -> nothing runnable


def test_independent_tasks_both_eligible(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "X", task_id="X", priority=5)
    tasks.create_task(env, "Y", task_id="Y", priority=5)
    # No deps → both are eligible; claiming one leaves the other for a 2nd worker
    t1 = tasks.next_task(env, claim=True, worker="w1")
    t2 = tasks.next_task(env, claim=True, worker="w2")
    assert {t1.id, t2.id} == {"X", "Y"}


def test_claim_is_exclusive(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "only", task_id="O", priority=1)
    t1 = tasks.next_task(env, claim=True, worker="w1")
    t2 = tasks.next_task(env, claim=True, worker="w2")
    assert t1.id == "O" and t1.claimed_by == "w1"
    assert t2 is None  # claimed by w1, in_progress → off-limits to w2


def test_worker_resumes_its_own_task(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "mine", task_id="M", priority=1)
    tasks.next_task(env, claim=True, worker="w1")
    again = tasks.next_task(env, claim=True, worker="w1")
    assert again.id == "M"  # same worker gets its in-progress task back


def test_unknown_dep_keeps_task_blocked(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "T", task_id="T", depends_on=["NOPE"], priority=1)
    assert tasks.next_task(env) is None


def test_release_frees_the_claim(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "R", task_id="R", priority=1)
    tasks.next_task(env, claim=True, worker="w1")
    assert tasks.release_task(env, "R", worker="w1") is True
    # released → status still in_progress but unclaimed; w2 can resume it
    t = tasks.next_task(env, claim=True, worker="w2")
    assert t.id == "R" and t.claimed_by == "w2"


def test_concurrent_claims_never_collide(tmp_path):
    env = _env(tmp_path)
    n = 12
    for i in range(n):
        tasks.create_task(env, f"task{i}", task_id=f"P{i:02d}", priority=1)

    # Each worker claims exactly ONE task (a realistic single-claim race). With
    # a unique worker id per thread, no two threads may end up on the same task.
    def claim_one(w):
        t = tasks.next_task(env, claim=True, worker=f"w{w}")
        return t.id if t else None

    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(claim_one, range(n)))

    claimed = [r for r in results if r]
    assert len(claimed) == n                      # all tasks went to a worker
    assert len(claimed) == len(set(claimed))      # no task claimed twice


def test_depends_on_round_trips_through_json(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "dep", task_id="D1", priority=1)
    tasks.create_task(env, "child", task_id="D2", depends_on=["D1"], priority=1)
    reloaded = tasks.find(env, "D2")
    assert reloaded.depends_on == ["D1"]


def test_runnable_tasks_counts_independent(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "A", task_id="A", priority=1)
    tasks.create_task(env, "B", task_id="B", priority=1)
    tasks.create_task(env, "C", task_id="C", depends_on=["A"], priority=1)
    # A and B runnable now; C blocked on A
    runnable = {t.id for t in tasks.runnable_tasks(env)}
    assert runnable == {"A", "B"}


def test_runnable_excludes_claimed(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "A", task_id="A", priority=1)
    tasks.create_task(env, "B", task_id="B", priority=1)
    tasks.next_task(env, claim=True, worker="w1")  # claims A
    runnable = {t.id for t in tasks.runnable_tasks(env)}
    assert runnable == {"B"}


def test_runnable_unblocks_after_dep_done(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "A", task_id="A", priority=1)
    tasks.create_task(env, "C", task_id="C", depends_on=["A"], priority=1)
    assert {t.id for t in tasks.runnable_tasks(env)} == {"A"}
    a = tasks.find(env, "A")
    tasks.accept(a); tasks.mark_done(tasks.find(env, "A"))
    assert {t.id for t in tasks.runnable_tasks(env)} == {"C"}
