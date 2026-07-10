"""Two (or ten) threads calling the same harn_env-writing MCP tool concurrently
must not corrupt the file or lose an entry — this is new risk as of Phase 3,
where multiple real agent processes can call these tools at the same instant
(parallel workflow steps / parallel-wave agents), not just one process writing
sequentially as before.
"""
from __future__ import annotations

import threading

from harn import scaffold, ENV_DIRNAME
from harn import tasks as tasks_mod


def _env(tmp_path):
    scaffold.setup(tmp_path)
    return tmp_path / ENV_DIRNAME


def test_concurrent_save_to_skill_does_not_lose_entries(tmp_path, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    from harn import mcp_server as ms
    srv = ms.build_server(start_watch=False)
    save_fn = next(t.fn for t in srv._tool_manager._tools.values()
                   if t.name == "save_to_skill")

    errors = []

    def worker(i):
        try:
            save_fn(skill="concurrency-test", content=f"entry-{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    body = (env / "skills" / "concurrency-test" / "SKILL.md").read_text()
    for i in range(10):
        assert f"entry-{i}" in body, f"entry-{i} lost to a concurrent-write race"


def _task_env(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    return env


def test_concurrent_record_decision_does_not_lose_entries(tmp_path):
    env = _task_env(tmp_path)
    task = tasks_mod.create_task(env, title="concurrency test task")

    errors = []

    def worker(i):
        try:
            # Each thread loads its OWN copy of the task, mimicking two
            # separate agent processes each holding a slightly-stale in-memory
            # Task read at slightly different times.
            t = tasks_mod.find(env, task.id)
            tasks_mod.record_decision(t, f"decision-{i}", rationale=f"because-{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    fresh = tasks_mod.find(env, task.id)
    got = {d.decision for d in fresh.decisions}
    for i in range(10):
        assert f"decision-{i}" in got, f"decision-{i} lost to a concurrent-write race"


def test_concurrent_record_change_does_not_lose_entries(tmp_path):
    env = _task_env(tmp_path)
    task = tasks_mod.create_task(env, title="concurrency test task 2")

    errors = []

    def worker(i):
        try:
            t = tasks_mod.find(env, task.id)
            tasks_mod.record_change(t, f"change-{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    fresh = tasks_mod.find(env, task.id)
    got = {c.summary for c in fresh.changelog}
    for i in range(10):
        assert f"change-{i}" in got, f"change-{i} lost to a concurrent-write race"
