"""Task JSON: id scheme, prds array, subtasks, next_id, find."""
from __future__ import annotations

import json
from pathlib import Path

from harn import ids, tasks
from .conftest import make_task


def test_is_tracker_key():
    assert ids.is_tracker_key("AUTH-42")
    assert ids.is_tracker_key("PRJ-001")
    assert not ids.is_tracker_key("plain-slug")
    assert not ids.is_tracker_key("auth")


def test_now_iso_has_second_precision():
    """`_now_iso()` timestamps every step_results started/ended entry the
    studio Activity view renders. Minute-only precision made concurrent
    parallel-wave steps (or several quick tool calls) indistinguishable in
    the UI -- a real, reported gap when diagnosing why a "simple" run took
    over two minutes: every timestamp in view read the same "HH:MM"."""
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", tasks._now_iso())


def test_task_with_multiple_prds(tmp_path):
    (tmp_path / "tasks").mkdir()
    path = tmp_path / "tasks" / "AUTH-42.json"
    path.write_text(json.dumps({
        "id": "AUTH-42",
        "title": "Auth + payments refactor",
        "status": "todo",
        "priority": 1,
        "prds": ["auth", "payments"],
        "epic": "Q3-INFRA",
        "user_story": "AUTH-10",
        "skills": ["security"],
        "subtasks": [],
        "description": "## What\nRefactor.\n## Done when\n- passing",
        "review_log": [],
    }))
    t = tasks.load_tasks(tmp_path)[0]
    assert t.id == "AUTH-42"
    assert t.prds == ["auth", "payments"]
    assert t.epic == "Q3-INFRA"
    assert t.user_story == "AUTH-10"


def test_subtasks_round_trip(tmp_path):
    (tmp_path / "tasks").mkdir()
    path = tmp_path / "tasks" / "PRJ-001.json"
    path.write_text(json.dumps({
        "id": "PRJ-001", "title": "T", "status": "todo", "priority": 1,
        "prds": [], "epic": None, "user_story": None, "skills": [],
        "subtasks": [
            {"id": "PRJ-001-1", "title": "Sub A", "status": "done"},
            {"id": "PRJ-001-2", "title": "Sub B", "status": "todo"},
        ],
        "description": "", "review_log": [],
    }))
    t = tasks.load_tasks(tmp_path)[0]
    assert len(t.subtasks) == 2
    assert t.subtasks[0].status == tasks.DONE
    assert t.subtasks[1].status == tasks.TODO


def test_review_log_round_trip(tmp_path):
    env = tmp_path
    t = make_task(env, "PRJ-001")
    tasks.submit_for_review(t, "claude", summary="done", tokens="5k")
    tasks.request_changes(t, "rename X")
    tasks.accept(t, notes="ship it")

    fresh = tasks.find(env, "PRJ-001")
    assert len(fresh.review_log) == 3
    assert fresh.review_log[0].tokens == "5k"
    assert fresh.review_log[1].comment == "rename X"
    assert fresh.review_log[2].notes == "ship it"


def test_find_by_id(tmp_path):
    make_task(tmp_path, "AUTH-42", title="Auth")
    assert tasks.find(tmp_path, "AUTH-42").title == "Auth"


def test_find_case_insensitive(tmp_path):
    make_task(tmp_path, "PRJ-001", title="Foo")
    assert tasks.find(tmp_path, "prj-001") is not None


def test_next_id_empty(tmp_path):
    (tmp_path / "tasks").mkdir()
    assert tasks.next_id(tmp_path, "PRJ") == "PRJ-001"


def test_next_id_skips_other_prefixes(tmp_path):
    make_task(tmp_path, "AUTH-010")
    make_task(tmp_path, "PRJ-003")
    assert tasks.next_id(tmp_path, "PRJ") == "PRJ-004"
    assert tasks.next_id(tmp_path, "AUTH") == "AUTH-011"
