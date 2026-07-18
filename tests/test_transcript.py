import json

from harn import transcript


def test_append_and_read_filters_by_task_step_and_cursor(tmp_path):
    env = tmp_path / "harn_env"
    first = transcript.append(
        env, task_id="PRJ-1", step_id="step-a", run_id="r-1", attempt=1,
        kind="status", phase="started", title="Starting", text="",
    )
    second = transcript.append(
        env, task_id="PRJ-1", step_id="step-b", run_id="r-1", attempt=1,
        kind="message", phase="completed", title="Agent", text="Done",
    )
    transcript.append(
        env, task_id="PRJ-2", step_id="step-a", run_id="r-2", attempt=1,
        kind="error", phase="failed", title="Error", text="Nope",
    )

    assert first["seq"] == 1
    assert second["seq"] == 2
    all_for_task = transcript.read(env, task_id="PRJ-1")
    assert [e["seq"] for e in all_for_task["entries"]] == [1, 2]
    assert all_for_task["cursor"] == 2
    only_step = transcript.read(env, task_id="PRJ-1", step_id="step-b")
    assert [e["text"] for e in only_step["entries"]] == ["Done"]
    unseen = transcript.read(env, task_id="PRJ-1", after=first["seq"])
    assert [e["seq"] for e in unseen["entries"]] == [2]


def test_attempts_are_retained_and_limit_is_enforced(tmp_path):
    env = tmp_path / "harn_env"
    for attempt in (1, 2, 3):
        transcript.append(
            env, task_id="PRJ-1", step_id="step-a", run_id=f"r-{attempt}",
            attempt=attempt, kind="message", phase="completed", title="Agent",
            text=f"attempt {attempt}", item_id=f"item-{attempt}",
        )

    page = transcript.read(env, task_id="PRJ-1", limit=2)
    assert [e["attempt"] for e in page["entries"]] == [1, 2]
    assert page["cursor"] == 2
    assert transcript.read(env, task_id="PRJ-1", after=2)["entries"][0]["attempt"] == 3


def test_read_skips_malformed_and_strips_unknown_fields(tmp_path):
    env = tmp_path / "harn_env"
    stored = transcript.append(
        env, task_id="PRJ-1", step_id="step-a", run_id="r-1", attempt=1,
        kind="tool", phase="completed", title="weather", text="21 C",
        item_id="tool-1",
    )
    path = env / "state" / "step_transcript.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({**stored, "seq": 99, "secret": "raw provider data"}) + "\n")

    result = transcript.read(env, task_id="PRJ-1")
    assert len(result["entries"]) == 2
    assert "secret" not in result["entries"][1]
    assert result["cursor"] == 99


def test_clear_task_removes_only_that_tasks_transcript(tmp_path):
    env = tmp_path / "harn_env"
    transcript.append(
        env, task_id="PRJ-1", step_id="step-a", run_id="r-1", attempt=1,
        kind="error", phase="failed", text="old failure",
    )
    kept = transcript.append(
        env, task_id="PRJ-2", step_id="step-b", run_id="r-2", attempt=1,
        kind="message", phase="completed", text="keep me",
    )

    assert transcript.clear_task(env, "PRJ-1") == 1
    assert transcript.read(env, task_id="PRJ-1")["entries"] == []
    remaining = transcript.read(env, task_id="PRJ-2")["entries"]
    assert [entry["text"] for entry in remaining] == ["keep me"]
    assert remaining[0]["seq"] == kept["seq"]
