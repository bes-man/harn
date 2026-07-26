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


# --- the human-in-the-loop pair belongs in the step's own feed ------------- #
# Reported live: the question showed only in the Run progress header and the
# comments list, so someone reading a step's activity scrolled the whole feed
# and never found it — and after answering, saw no trace of their own answer
# either.

def test_an_answer_is_written_into_the_steps_feed(tmp_path):
    from harn import loop, state, transcript, ENV_DIRNAME
    env = tmp_path / ENV_DIRNAME
    (env / "state").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    from harn import tasks
    t = tasks.create_task(env, "Do it", task_id="PRJ-001")

    state.blocked_marker(env / "state").write_text("Which one?", encoding="utf-8")
    st = state.State.load(env / "state")
    st.current_task = t.id
    st.current_step = "step-1"
    st.block("Which one?")
    st.save(env / "state")

    loop.answer(env, "the second one", source="studio")

    entries = transcript.read(env, task_id=t.id)["entries"]
    answers = [e for e in entries if e.get("kind") == "answer"]
    assert len(answers) == 1
    assert answers[0]["text"] == "the second one"
    assert answers[0]["step_id"] == "step-1"
    assert "studio" in answers[0]["title"]


def test_studio_renders_the_question_and_answer_kinds():
    from harn import studio
    html = studio._HTML
    assert "question:'？',answer:'✎'" in html


def test_a_question_stays_in_the_current_attempt_group(tmp_path):
    """Studio collapses every attempt group except the highest, so an entry
    written with a LOWER number is buried in a collapsed block instead of
    sitting at the end where it just happened — reported as "the question
    isn't at the end of the feed"."""
    from harn import scaffold, state, tasks, transcript, ENV_DIRNAME
    import os
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    t = tasks.create_task(env, "T", task_id="PRJ-001")
    t.step_results["s1"] = {"status": "running", "attempts": 2}
    tasks._save(t)
    for a, txt in ((1, "first try"), (1, "first try failed"), (2, "second try")):
        transcript.append(env, task_id="PRJ-001", step_id="s1", run_id="r",
                          attempt=a, kind="message", phase="completed",
                          title="agent", text=txt)

    st = state.State.load(env / "state")
    st.current_task = t.id
    st.current_step = "s1"
    st.save(env / "state")

    from harn import loop
    state.blocked_marker(env / "state").write_text("Which one?", encoding="utf-8")
    st = state.State.load(env / "state")
    st.block("Which one?")
    st.save(env / "state")
    loop.answer(env, "the second", source="studio")

    entries = transcript.read(env, task_id="PRJ-001")["entries"]
    answer = next(e for e in entries if e["kind"] == "answer")
    # `loop.answer` resets the ledger's attempts to 0 on purpose (fresh
    # budget), so trusting it alone would file this under attempt 1.
    assert answer["attempt"] == 2
    assert entries[-1]["kind"] == "answer"      # genuinely last


def test_latest_attempt_ignores_other_steps_and_tasks(tmp_path):
    from harn import transcript, ENV_DIRNAME
    env = tmp_path / ENV_DIRNAME
    transcript.append(env, task_id="A", step_id="s1", run_id="r", attempt=5,
                      kind="message", phase="completed", text="elsewhere")
    transcript.append(env, task_id="B", step_id="s1", run_id="r", attempt=2,
                      kind="message", phase="completed", text="mine")
    assert transcript.latest_attempt(env, "B", "s1") == 2
    assert transcript.latest_attempt(env, "B", "s2") == 0
