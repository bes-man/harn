"""Context-read tracking: what actually entered the agent's context for a task,
as opposed to what was merely AVAILABLE (skill/tool names in the prompt).

`read_skill`/`read_service`/`read_prd`/`read_guidance` tag a `context_read`
event to STATE.json's current_task, which `get_next_task` now sets
unconditionally on claim (previously only in the low-autonomy block)."""
from __future__ import annotations

from pathlib import Path

from harn import events, state, tasks, studio, ENV_DIRNAME


def _env(tmp_path: Path) -> Path:
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    (env / "skills").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    (env / "services").mkdir(parents=True)
    return env


def _tool_fn(env: Path, name: str, monkeypatch):
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    from harn import mcp_server as ms
    server = ms.build_server()
    return next(t.fn for t in server._tool_manager._tools.values() if t.name == name)


def _write_skill(env, name, body="# body"):
    d = env / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}\n",
                                 encoding="utf-8")


def _set_current_task(env, task_id):
    st = state.State.load(env / "state")
    st.current_task = task_id
    st.save(env / "state")


def test_read_skill_tags_current_task(tmp_path, monkeypatch):
    env = _env(tmp_path)
    _write_skill(env, "testing")
    _set_current_task(env, "PRJ-001")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "read_skill", monkeypatch)
    fn(name="testing")

    evs = events.read(env, task_id="PRJ-001")
    reads = [e for e in evs if e["event"] == "context_read"]
    assert len(reads) == 1
    assert reads[0]["kind"] == "skill" and reads[0]["name"] == "testing"


def test_read_skill_untagged_without_current_task(tmp_path, monkeypatch):
    """No claimed task -> nothing to attribute the read to -> no context_read
    event (build_server() itself always logs an unrelated run_start)."""
    env = _env(tmp_path)
    _write_skill(env, "testing")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "read_skill", monkeypatch)
    fn(name="testing")

    assert [e for e in events.read(env) if e["event"] == "context_read"] == []


def test_read_skill_missing_skill_not_tagged(tmp_path, monkeypatch):
    env = _env(tmp_path)
    _set_current_task(env, "PRJ-001")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "read_skill", monkeypatch)
    fn(name="nonexistent")

    assert [e for e in events.read(env) if e["event"] == "context_read"] == []


def test_read_service_tags_current_task(tmp_path, monkeypatch):
    env = _env(tmp_path)
    (env / "services" / "auth-api.md").write_text(
        "---\nresponsibility: owns login\n---\nbody", encoding="utf-8")
    _set_current_task(env, "PRJ-002")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "read_service", monkeypatch)
    fn(name="auth-api")

    reads = [e for e in events.read(env, task_id="PRJ-002") if e["event"] == "context_read"]
    assert len(reads) == 1 and reads[0]["kind"] == "service"


def test_read_prd_tags_current_task(tmp_path, monkeypatch):
    env = _env(tmp_path)
    (env / "prd").mkdir(parents=True)
    (env / "prd" / "auth.md").write_text("# Auth\n## Problem\nneed login",
                                          encoding="utf-8")
    _set_current_task(env, "PRJ-003")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "read_prd", monkeypatch)
    fn(slug="auth")

    reads = [e for e in events.read(env, task_id="PRJ-003") if e["event"] == "context_read"]
    assert len(reads) == 1 and reads[0]["kind"] == "prd" and reads[0]["name"] == "auth"


def test_read_guidance_tags_current_task(tmp_path, monkeypatch):
    env = _env(tmp_path)
    (env / "guidance").mkdir(parents=True)
    (env / "guidance" / "hil.md").write_text("# HIL", encoding="utf-8")
    _set_current_task(env, "PRJ-004")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "read_guidance", monkeypatch)
    fn(topic="hil")

    reads = [e for e in events.read(env, task_id="PRJ-004") if e["event"] == "context_read"]
    assert len(reads) == 1 and reads[0]["kind"] == "guidance" and reads[0]["name"] == "hil"


def test_get_next_task_sets_current_task_even_at_normal_autonomy(tmp_path, monkeypatch):
    """Regression: previously current_task was only set in the autonomy<=0.3
    block, so context reads in ordinary chat mode were never attributable."""
    env = _env(tmp_path)
    tasks.create_task(env, "Add auth", task_id="PRJ-005")
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "get_next_task", monkeypatch)
    fn(worker="w1")

    st = state.State.load(env / "state")
    assert st.current_task == "PRJ-005"


def test_board_payload_surfaces_context_reads(tmp_path, monkeypatch):
    env = _env(tmp_path)
    _write_skill(env, "testing")
    t = tasks.create_task(env, "Add auth")
    _set_current_task(env, t.id)
    monkeypatch.setattr("harn.mcp_server._ensure_watch_running", lambda e: None)

    fn = _tool_fn(env, "read_skill", monkeypatch)
    fn(name="testing")

    payload = studio.board_payload(env)
    row = next(r for r in payload["tasks"] if r["id"] == t.id)
    assert row["context_reads"] == [
        {"kind": "skill", "name": "testing", "ts": row["context_reads"][0]["ts"]}]


def test_board_payload_empty_context_reads_for_untouched_task(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    payload = studio.board_payload(env)
    row = next(r for r in payload["tasks"] if r["id"] == t.id)
    assert row["context_reads"] == []
