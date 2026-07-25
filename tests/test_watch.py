"""harn watch — the dispatcher: oracle headless on review, live feed, HIL routing."""
from __future__ import annotations

from pathlib import Path

from harn import loop, tasks, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


def _env(tmp_path: Path, *, oracle=True, auto_reconcile=False) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        f"[loop]\noracle = {str(oracle).lower()}\n"
        f"auto_reconcile = {str(auto_reconcile).lower()}\n"
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def _wire(monkeypatch, oracle_text="ORACLE: PASS"):
    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd): return AgentResult(ok=True, text=oracle_text)
    monkeypatch.setattr(loop, "get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr(loop, "_git_diff", lambda root: "diff --git a/x b/x")
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_watch_runs_oracle_on_review_task(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=True)
    t = make_task(env, "PRJ-001", status=tasks.REVIEW)
    _wire(monkeypatch, "looks good\nORACLE: PASS")

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)

    fresh = tasks.find(env, "PRJ-001")
    assert any(e.event == "oracle_pass" or e.event.startswith("oracle")
               for e in fresh.review_log) or fresh.status == tasks.REVIEW
    # PASS keeps it in review (for human); progress logged
    from harn import progress
    assert "oracle PASS" in progress.tail(env)


def test_watch_oracle_fail_sends_back_to_changes(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=True)
    make_task(env, "PRJ-001", status=tasks.REVIEW)
    _wire(monkeypatch, "missing TTL\nORACLE: FAIL — no TTL on tokens")

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)

    fresh = tasks.find(env, "PRJ-001")
    assert fresh.status == tasks.CHANGES_REQUESTED
    assert any(e.event == "oracle_fail" for e in fresh.review_log)


def test_watch_skips_already_oracled(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=True)
    t = make_task(env, "PRJ-001", status=tasks.REVIEW)
    t.review_log.append(tasks.ReviewEntry(ts="t", event="oracle_pass", agent="x"))
    tasks._save(t)
    calls = {"n": 0}

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd):
            calls["n"] += 1
            return AgentResult(ok=True, text="ORACLE: PASS")
    monkeypatch.setattr(loop, "get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr(loop, "_git_diff", lambda root: "")
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)
    assert calls["n"] == 0  # already had an oracle verdict → not re-run


def test_watch_oracle_off_does_nothing(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=False)
    make_task(env, "PRJ-001", status=tasks.REVIEW)
    calls = {"n": 0}

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd):
            calls["n"] += 1
            return AgentResult(ok=True, text="ORACLE: PASS")
    monkeypatch.setattr(loop, "get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)
    assert calls["n"] == 0


def test_oracle_review_standalone(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=True)
    t = make_task(env, "PRJ-001", status=tasks.REVIEW)
    _wire(monkeypatch, "great\nORACLE: PASS")
    from harn.config import Config
    verdict, detail, res = loop.oracle_review(env, Config.load(env), t, tmp_path)
    assert verdict == "PASS"


# --------------------------------------------------------------------------- #
# auto-reconcile: knowledge capture runs headless, no agent discipline needed
# --------------------------------------------------------------------------- #
def test_watch_auto_reconciles_review_task(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=False, auto_reconcile=True)
    make_task(env, "PRJ-001", status=tasks.REVIEW)
    calls = {"n": 0}

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd):
            calls["n"] += 1
            assert "ask_user" in prompt  # reconcile prompt; told NOT to use it
            return AgentResult(ok=True, text="RECONCILE: DONE")
    monkeypatch.setattr(loop, "get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)

    fresh = tasks.find(env, "PRJ-001")
    assert calls["n"] == 1                                   # reconcile turn ran
    assert any(e.event == "reconciled" for e in fresh.review_log)
    assert fresh.changelog                                   # documentation backstop fired


def test_watch_skips_already_reconciled(tmp_path, monkeypatch):
    env = _env(tmp_path, oracle=False, auto_reconcile=True)
    t = make_task(env, "PRJ-001", status=tasks.REVIEW)
    t.review_log.append(tasks.ReviewEntry(ts="t", event="reconciled", agent="x"))
    tasks._save(t)
    calls = {"n": 0}

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd):
            calls["n"] += 1
            return AgentResult(ok=True, text="x")
    monkeypatch.setattr(loop, "get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)
    assert calls["n"] == 0  # already reconciled → not re-run


class RecordingAdapter:
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        return AgentResult(ok=True, text="done")


# --- resuming after a Telegram/auto answer -------------------------------- #
# Regression: a block raised by a process that had ALREADY EXITED by the time
# watch() noticed it (the attempt-cap block returns immediately rather than
# waiting in-process the way a genuine ask_user block does) left `answer()`'s
# own promise -- "resume on next `harn run`" -- unfulfilled. Nothing else was
# ever going to make that next run happen: a human answered in Telegram
# (including pressing "Decide for me") and the UI never moved.

class _FakeHILAnswers:
    """Mimics TelegramHIL.await_answer's contract without any network."""
    def __init__(self, reply, source):
        self._reply, self._source = reply, source
    def await_answer(self, question, **kw):
        return self._reply, self._source
    def poll_updates(self, state_dir):
        return {"commands": [], "documents": []}
    def wait_for_reply(self, text, **kw):
        return None   # the run this triggers reaches the review gate too;
                       # None here means "not answered" -> falls back to CLI


def _blocked_task_with_no_active_run(env, tmp_path, *, claimed_by=None):
    from harn import state, workflows
    t = make_task(env, "PRJ-001", status=tasks.IN_PROGRESS)
    if claimed_by:
        t.claimed_by = claimed_by
        tasks._save(t)
    workflows.save_task_plan(env, t.id, {"preamble": "", "nodes": [
        {"kind": "step", "id": "step-1", "title": "Step 1", "enabled": True},
    ]})
    detail = "Step 'Step 1' (step-1) stopped after 2 unsuccessful attempts…"
    (env / "state").mkdir(parents=True, exist_ok=True)
    state.blocked_marker(env / "state").write_text(detail, encoding="utf-8")
    st = state.State.load(env / "state")
    st.current_task = t.id
    st.block(detail)
    st.save(env / "state")
    return t


def test_decide_for_me_resumes_a_task_whose_process_already_exited(tmp_path, monkeypatch):
    env = _env(tmp_path)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        '[notify]\nwait_for_reply = true\nhil_channel = "telegram"\n', encoding="utf-8")
    t = _blocked_task_with_no_active_run(env, tmp_path)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop.TelegramHIL, "from_env",
                       staticmethod(lambda *_: _FakeHILAnswers(None, "auto")))

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)

    # The task actually ran again -- not just recorded an answer nobody acts on.
    assert len(fake.calls) >= 1
    fresh = tasks.find(env, t.id)
    assert fresh.step_results.get("step-1", {}).get("status") == "ok"


def test_decide_for_me_uses_the_claimed_role_when_the_task_has_one(tmp_path, monkeypatch):
    env = _env(tmp_path)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        '[notify]\nwait_for_reply = true\nhil_channel = "telegram"\n', encoding="utf-8")
    (env / "agents").mkdir(parents=True, exist_ok=True)
    from harn import roles
    roles.save(env, {"name": "spec-writer", "command": "spec",
                     "status": tasks.IN_PROGRESS, "trigger": "manual"})
    t = _blocked_task_with_no_active_run(env, tmp_path, claimed_by="spec-writer")
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop.TelegramHIL, "from_env",
                       staticmethod(lambda *_: _FakeHILAnswers(None, "auto")))

    loop.watch(env, tmp_path, _once=True, _sleep=lambda s: None)

    assert len(fake.calls) >= 1
    fresh = tasks.find(env, t.id)
    assert fresh.claimed_by == "spec-writer"


def test_resume_after_answer_is_a_noop_when_a_run_is_still_active(tmp_path, monkeypatch):
    """A block from a STILL-ALIVE process (genuine ask_user, waiting
    in-process) must not be double-run just because watch() also saw the
    marker -- that process's own poll will continue it on its own."""
    env = _env(tmp_path)
    t = _blocked_task_with_no_active_run(env, tmp_path)
    from harn import runstate
    runstate.begin(env, t.id, pid=__import__("os").getpid())

    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    from harn.config import Config
    loop._resume_after_answer(tmp_path, env, t.id, cfg=Config.load(env))

    assert fake.calls == []
