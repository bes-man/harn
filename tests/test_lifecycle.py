"""Task lifecycle: status transitions, pick order, review filter, board."""
from __future__ import annotations

import json
from pathlib import Path

from harn import tasks
from .conftest import make_task


def _env(tmp_path: Path) -> Path:
    env = tmp_path / "harn_env"
    env.mkdir()
    return env


def test_full_track(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001", title="Add feature")
    assert t.status == tasks.TODO

    tasks.set_status(t, tasks.IN_PROGRESS)
    assert tasks.find(env, "PRJ-001").status == tasks.IN_PROGRESS

    tasks.submit_for_review(t, "claude", summary="done")
    assert tasks.find(env, "PRJ-001").status == tasks.REVIEW

    tasks.request_changes(t, "needs tests", by="user")
    assert tasks.find(env, "PRJ-001").status == tasks.CHANGES_REQUESTED

    tasks.accept(t, notes="looks good", by="user")
    assert tasks.find(env, "PRJ-001").status == tasks.DONE


def test_review_log_is_structured(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.submit_for_review(t, "claude", summary="impl", tokens="1k")
    tasks.request_changes(t, "refactor", by="user")
    tasks.accept(t, notes="nice", by="user")

    fresh = tasks.find(env, "PRJ-001")
    events = [e.event for e in fresh.review_log]
    assert events == ["submitted_for_review", "changes_requested", "accepted"]
    assert fresh.review_log[0].tokens == "1k"
    assert fresh.review_log[1].comment == "refactor"
    assert fresh.review_log[2].notes == "nice"


def test_pick_order_resume_then_rework_then_new(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-001", status=tasks.IN_PROGRESS, priority=5)
    make_task(env, "PRJ-002", status=tasks.CHANGES_REQUESTED, priority=1)
    make_task(env, "PRJ-003", status=tasks.TODO, priority=1)

    first = tasks.next_task(env)
    assert first.id == "PRJ-001"

    second = tasks.next_task(env, exclude={"PRJ-001"})
    assert second.id == "PRJ-002"

    third = tasks.next_task(env, exclude={"PRJ-001", "PRJ-002"})
    assert third.id == "PRJ-003"


def test_review_filter(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-001", status=tasks.REVIEW)
    make_task(env, "PRJ-002", status=tasks.TODO)

    in_review = tasks.tasks_in_review(env)
    assert len(in_review) == 1
    assert in_review[0].id == "PRJ-001"


def test_board_shows_all_statuses(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-001", title="Alpha", status=tasks.TODO)
    make_task(env, "PRJ-002", title="Beta", status=tasks.DONE)

    b = tasks.board(env)
    assert "Alpha" in b and "Beta" in b
    assert "todo" in b and "done" in b


def test_subtasks_shown_in_board(tmp_path):
    env = _env(tmp_path)
    p = env / "tasks" / "PRJ-001.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "id": "PRJ-001", "title": "Big task", "status": "todo",
        "priority": 1, "prds": [], "epic": None, "user_story": None,
        "skills": [], "description": "",
        "subtasks": [
            {"id": "PRJ-001-1", "title": "sub a", "status": "done"},
            {"id": "PRJ-001-2", "title": "sub b", "status": "todo"},
        ],
        "review_log": [],
    }))
    b = tasks.board(env)
    assert "1/2 subtasks" in b


def test_next_id_auto_increments(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-001")
    make_task(env, "PRJ-002")
    assert tasks.next_id(env, "PRJ") == "PRJ-003"


def test_next_id_with_gaps(tmp_path):
    env = _env(tmp_path)
    make_task(env, "PRJ-005")
    assert tasks.next_id(env, "PRJ") == "PRJ-006"


def test_create_task_writes_json(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(
        env, "Add cache",
        prds=["perf"], skills=["standards"],
        description="## What\nCache.\n## Done when\n- fast",
    )
    assert t.id == "PRJ-001"
    fresh = tasks.find(env, "PRJ-001")
    assert fresh.title == "Add cache"
    assert fresh.prds == ["perf"]
    assert fresh.skills == ["standards"]
