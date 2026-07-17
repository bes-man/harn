"""Task context markdown + real capture (spec B): the <id>.md + <id>.state.json
file split, append-only Context capture, board()'s bounded frontmatter-only
read, and lazy migration from the old single-JSON-blob task file."""
from __future__ import annotations

import json
import time
from pathlib import Path

from harn import tasks
from .conftest import make_task


def _env(tmp_path: Path) -> Path:
    env = tmp_path / "harn_env"
    (env / "tasks").mkdir(parents=True)
    return env


# ---------------------------------------------------------------------------
# file split: .md (frontmatter + prose) + .state.json (engine bookkeeping)
# ---------------------------------------------------------------------------
def test_create_task_writes_md_and_state_json_pair(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add cache", description="## What\nCache it.")
    md_path = env / "tasks" / f"{t.id}.md"
    state_path = env / "tasks" / f"{t.id}.state.json"
    assert md_path.exists()
    assert state_path.exists()
    assert not (env / "tasks" / f"{t.id}.json").exists()

    text = md_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert 'id: "PRJ-001"' in text
    assert "## Description" in text
    assert "## Context" in text
    assert text.rstrip("\n").endswith("## Context")   # nothing captured yet

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "step_results" in state and "claimed_by" in state and "skills" in state
    assert "description" not in state    # prose stays out of the sidecar


def test_frontmatter_and_body_round_trip_every_field(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(
        env, "Multi-field", prds=["auth", "billing"], priority=5,
        skills=["security"], epic="Q3", user_story="AUTH-1",
        depends_on=["PRJ-000"], workflow="fast-track",
        description="## What\nDo the thing.\n\n## Done when\n- criterion",
    )
    tasks.record_decision(t, "use JWT", rationale="stateless", agent="planner")
    tasks.submit_for_review(t, "claude", summary="shipped it", tokens="3k")
    tasks.record_change(t, "added /login", detail="see PR #4")

    fresh = tasks.find(env, t.id)
    assert fresh.title == "Multi-field"
    assert fresh.prds == ["auth", "billing"]
    assert fresh.priority == 5
    assert fresh.depends_on == ["PRJ-000"]
    assert fresh.workflow == "fast-track"
    assert fresh.description.startswith("## What\nDo the thing.")
    assert fresh.result == "shipped it"
    assert len(fresh.decisions) == 1
    assert fresh.decisions[0].decision == "use JWT"
    assert fresh.decisions[0].rationale == "stateless"
    assert len(fresh.review_log) == 1
    assert fresh.review_log[0].tokens == "3k"
    assert len(fresh.changelog) == 1
    assert fresh.changelog[0].detail == "see PR #4"
    # sidecar-only fields
    assert fresh.epic == "Q3"
    assert fresh.user_story == "AUTH-1"
    assert fresh.skills == ["security"]


def test_description_with_its_own_hash_headings_survives_round_trip(tmp_path):
    """The Description/Result content routinely has its own '## What' / '##
    Done when' sub-headings — these must not be mistaken for new top-level
    sections when the file is re-parsed."""
    env = _env(tmp_path)
    t = tasks.create_task(env, "Nested headings", task_id="PRJ-001")
    tasks.lock_spec(t, "- POST /login returns JWT\n- 401 on bad creds",
                    approach="bcrypt + jose",
                    decisions=[("15m token TTL", "security")])
    fresh = tasks.find(env, "PRJ-001")
    assert "POST /login returns JWT" in fresh.description
    assert "## Approach (locked)" in fresh.description
    assert fresh.spec_locked is True
    assert fresh.decisions[0].decision == "15m token TTL"


# ---------------------------------------------------------------------------
# Context: append-only, capped, capture survives header rewrites
# ---------------------------------------------------------------------------
def test_append_context_adds_a_heading_and_the_captured_text(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Weather task", task_id="PRJ-001")
    tasks.append_context(env, "PRJ-001", step_id="step-000001",
                         step_title="Check rate", text="the captured body")

    fresh = tasks.find(env, "PRJ-001")
    assert "step-000001" in fresh.context
    assert "Check rate" in fresh.context
    assert "the captured body" in fresh.context


def test_context_is_capped_with_truncation_marker(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "Noisy", task_id="PRJ-001")
    tasks.append_context(env, "PRJ-001", step_id="step-1", text="y" * 9000)
    fresh = tasks.find(env, "PRJ-001")
    assert len(fresh.context) < 9000
    assert "(truncated)" in fresh.context


def test_context_survives_a_header_rewrite(tmp_path):
    """A frontmatter/description/decision mutation (a full read-modify-write)
    must preserve whatever was already appended to Context, not clobber it —
    this is what keeps '## Context' append-only for the task's lifetime."""
    env = _env(tmp_path)
    t = tasks.create_task(env, "Task", task_id="PRJ-001")
    tasks.append_context(env, "PRJ-001", step_id="step-1", text="first entry")

    fresh = tasks.find(env, "PRJ-001")
    tasks.record_decision(fresh, "some decision")     # full header rewrite

    reloaded = tasks.find(env, "PRJ-001")
    assert "first entry" in reloaded.context
    assert reloaded.decisions[0].decision == "some decision"


def test_append_context_is_a_true_append_not_a_full_rewrite(tmp_path, monkeypatch):
    env = _env(tmp_path)
    tasks.create_task(env, "Task", task_id="PRJ-001")
    md_path = env / "tasks" / "PRJ-001.md"

    calls = {"write_text": 0}
    orig_write_text = Path.write_text

    def spy_write_text(self, *a, **kw):
        if self == md_path:
            calls["write_text"] += 1
        return orig_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    tasks.append_context(env, "PRJ-001", step_id="step-1", text="entry one")
    tasks.append_context(env, "PRJ-001", step_id="step-2", text="entry two")
    assert calls["write_text"] == 0     # both appends used open(path, "a")

    text = md_path.read_text(encoding="utf-8")
    assert "entry one" in text and "entry two" in text


def test_append_context_noop_for_missing_task(tmp_path):
    env = _env(tmp_path)
    tasks.append_context(env, "NOPE-1", step_id="step-1", text="x")   # no crash


def test_append_context_noop_for_blank_text(tmp_path):
    env = _env(tmp_path)
    tasks.create_task(env, "Task", task_id="PRJ-001")
    tasks.append_context(env, "PRJ-001", step_id="step-1", text="   ")
    fresh = tasks.find(env, "PRJ-001")
    assert fresh.context == ""


# ---------------------------------------------------------------------------
# board(): bounded frontmatter (+small sidecar) read — a huge Context section
# must not appear in, or slow down, board()'s output
# ---------------------------------------------------------------------------
def test_board_does_not_include_context_content(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-001", title="Alpha")
    tasks.append_context(env, "PRJ-001", step_id="step-1",
                         text="SENTINEL_CONTEXT_TEXT_SHOULD_NOT_APPEAR")
    b = tasks.board(env)
    assert "Alpha" in b
    assert "SENTINEL_CONTEXT_TEXT_SHOULD_NOT_APPEAR" not in b


def test_board_stays_fast_with_a_large_context_section(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-001", title="Alpha")
    for i in range(200):
        tasks.append_context(env, "PRJ-001", step_id=f"step-{i}",
                             text=f"chunk {i} " + ("z" * 3000))
    t0 = time.time()
    for _ in range(20):
        tasks.board(env)
    elapsed = time.time() - t0
    assert elapsed < 2.0


def test_light_load_used_by_board_has_no_context_or_prose(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-001", title="Alpha",
             description="## What\nsecret body text\n")
    tasks.append_context(env, "PRJ-001", step_id="step-1", text="captured")
    light_tasks = tasks._load_tasks_light(env)
    assert light_tasks[0].context == ""
    assert light_tasks[0].description == ""


# ---------------------------------------------------------------------------
# migration: old single-JSON-blob <id>.json → <id>.md + <id>.state.json
# ---------------------------------------------------------------------------
def _write_legacy_json(env: Path, task_id: str, **overrides) -> Path:
    d = {
        "id": task_id, "title": "Legacy task", "status": "in_progress",
        "priority": 3, "prds": ["auth"], "epic": "Q1", "user_story": "AUTH-9",
        "skills": ["security"], "subtasks": [
            {"id": f"{task_id}-1", "title": "sub a", "status": "done"},
        ],
        "description": "## What\nold-format task\n",
        "scratchpad": "working note", "decisions": [
            {"decision": "use bcrypt", "rationale": "standard", "ts": "t1",
             "agent": "claude"}],
        "changelog": [{"ts": "t1", "summary": "did X", "detail": ""}],
        "baseline_ref": "abc123", "review_log": [
            {"ts": "t1", "event": "started", "agent": "claude"}],
        "depends_on": [], "claimed_by": "worker-1", "claimed_at": "t0",
        "spec_locked": True, "workflow": "fast-track",
        "workflow_confirmed": True,
        "stage_checkpoints": {"step-1": "ref1"},
        "task_patch_refs": ["ref-a"],
        "step_results": {"step-1": {"status": "ok"}},
    }
    d.update(overrides)
    path = env / "tasks" / f"{task_id}.json"
    path.write_text(json.dumps(d), encoding="utf-8")
    return path


def test_migration_splits_legacy_json_into_md_and_state(tmp_path):
    env = _env(tmp_path)
    legacy_path = _write_legacy_json(env, "PRJ-001")

    fresh = tasks.find(env, "PRJ-001")
    assert not legacy_path.exists()
    assert (env / "tasks" / "PRJ-001.md").exists()
    assert (env / "tasks" / "PRJ-001.state.json").exists()

    assert fresh.title == "Legacy task"
    assert fresh.status == "in_progress"
    assert fresh.priority == 3
    assert fresh.prds == ["auth"]
    assert fresh.epic == "Q1"
    assert fresh.user_story == "AUTH-9"
    assert fresh.skills == ["security"]
    assert fresh.subtasks[0].status == "done"
    assert fresh.description == "## What\nold-format task"
    assert fresh.scratchpad == "working note"
    assert fresh.decisions[0].decision == "use bcrypt"
    assert fresh.changelog[0].summary == "did X"
    assert fresh.baseline_ref == "abc123"
    assert fresh.review_log[0].event == "started"
    assert fresh.claimed_by == "worker-1"
    assert fresh.spec_locked is True
    assert fresh.workflow == "fast-track"
    assert fresh.workflow_confirmed is True
    assert fresh.stage_checkpoints == {"step-1": "ref1"}
    assert fresh.task_patch_refs == ["ref-a"]
    assert fresh.step_results == {"step-1": {"status": "ok"}}


def test_migration_is_idempotent_once_done(tmp_path):
    env = _env(tmp_path)
    _write_legacy_json(env, "PRJ-001")
    tasks.find(env, "PRJ-001")             # triggers migration
    again = tasks.find(env, "PRJ-001")     # second read: already migrated
    assert again is not None
    assert again.title == "Legacy task"


def test_migrated_task_is_then_fully_usable(tmp_path):
    env = _env(tmp_path)
    _write_legacy_json(env, "PRJ-001", status="todo", claimed_by=None)
    t = tasks.find(env, "PRJ-001")
    tasks.submit_for_review(t, "claude", summary="done now")
    fresh = tasks.find(env, "PRJ-001")
    assert fresh.status == tasks.REVIEW
    assert fresh.result == "done now"
