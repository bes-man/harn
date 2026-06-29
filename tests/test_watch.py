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
