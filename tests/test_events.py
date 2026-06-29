"""Structured event stream: emit/read/metrics + loop & MCP wiring (observability)."""
from __future__ import annotations

from pathlib import Path

from harn import events, loop, tasks, state, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from tests.conftest import make_task


# --- module: emit / read / run_id ------------------------------------------ #

def test_new_run_mints_id_and_emits_run_start(tmp_path):
    env = tmp_path / ENV_DIRNAME
    rid = events.new_run(env, kind="loop")
    assert rid.startswith("r-")
    assert events.current_run(env) == rid
    evs = events.read(env)
    assert len(evs) == 1
    assert evs[0]["event"] == "run_start"
    assert evs[0]["run_id"] == rid
    assert evs[0]["kind"] == "loop"


def test_emit_stamps_run_and_drops_none(tmp_path):
    env = tmp_path / ENV_DIRNAME
    rid = events.new_run(env)
    events.emit(env, "stage_end", task_id="PRJ-1", stage="execute",
                verdict=None, dur_ms=120)
    e = events.read(env)[-1]
    assert e["run_id"] == rid
    assert e["task_id"] == "PRJ-1"
    assert "verdict" not in e        # None dropped
    assert e["dur_ms"] == 120
    assert e["ts"].endswith("Z")


def test_read_filters_by_task_and_run(tmp_path):
    env = tmp_path / ENV_DIRNAME
    events.new_run(env)
    events.emit(env, "stage_end", task_id="A", stage="execute")
    events.emit(env, "stage_end", task_id="B", stage="execute")
    assert len(events.read(env, task_id="A")) == 1
    assert len(events.read(env, task_id="B")) == 1
    rid = events.current_run(env)
    assert all(e["run_id"] == rid for e in events.read(env, run_id=rid))


def test_read_skips_malformed_lines(tmp_path):
    env = tmp_path / ENV_DIRNAME
    events.new_run(env)
    (env / "state" / "events.jsonl").open("a").write("not json\n")
    # still reads the valid run_start
    assert any(e["event"] == "run_start" for e in events.read(env))


def test_emit_is_best_effort_no_crash(tmp_path, monkeypatch):
    env = tmp_path / ENV_DIRNAME
    # force the open to raise; emit must swallow it
    import builtins
    real_open = builtins.open

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "open", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    events.emit(env, "stage_end", task_id="X")  # must not raise


# --- metrics --------------------------------------------------------------- #

def test_metrics_aggregates(tmp_path):
    env = tmp_path / ENV_DIRNAME
    events.new_run(env)
    events.emit(env, "stage_end", task_id="A", stage="execute",
                tok_in=100, tok_out=50, cost_usd=0.01, dur_ms=200)
    events.emit(env, "stage_end", task_id="A", stage="execute",
                tok_in=100, tok_out=50, dur_ms=400)
    events.emit(env, "stage_end", task_id="A", stage="oracle", verdict="PASS",
                tok_in=80, tok_out=20)
    events.emit(env, "block", task_id="A")
    events.emit(env, "cycle_end", task_id="A", outcome="submitted")
    m = events.metrics(env)
    assert m["runs"] == 1 and m["cycles"] == 1 and m["blocks"] == 1
    assert m["tok_in"] == 280 and m["tok_out"] == 120
    assert m["stages"]["execute"]["count"] == 2
    assert m["stages"]["execute"]["mean_dur_ms"] == 300
    assert m["oracle"]["PASS"] == 1


# --- loop integration: every cycle leaves a durable log -------------------- #

class FakeAdapter:
    name = "fake"

    def __init__(self, text="did the work"):
        self.text = text
        self.calls = 0

    def available(self):
        return True

    def run_turn(self, prompt, cwd):
        self.calls += 1
        return AgentResult(ok=True, text=self.text,
                           input_tokens=10, output_tokens=5)


def _env_with_task(tmp_path: Path) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Feat", priority=1,
              description="## What\nBuild.\n\n## Done when\n- works")
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        "[loop]\nmax_iterations = 6\nverify = false\nplanning = false\noracle = false\n"
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def test_loop_emits_run_stage_and_cycle_events(tmp_path, monkeypatch):
    env = _env_with_task(tmp_path)
    fake = FakeAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)

    evs = events.read(env)
    kinds = [e["event"] for e in evs]
    assert "run_start" in kinds
    assert "stage_end" in kinds          # a completed agent cycle is logged
    assert "cycle_end" in kinds          # the task work-cycle is logged
    assert "run_end" in kinds            # the run span is closed
    # the execution stage_end carries token + duration telemetry
    se = next(e for e in evs if e["event"] == "stage_end" and e["stage"] == "execute")
    assert se["task_id"] == "PRJ-001"
    assert se["tok_in"] == 10 and se["tok_out"] == 5
    assert "dur_ms" in se
    # all events of the run share one run_id
    rids = {e["run_id"] for e in evs}
    assert len(rids) == 1 and next(iter(rids)).startswith("r-")


def test_loop_run_end_carries_final_phase(tmp_path, monkeypatch):
    env = _env_with_task(tmp_path)
    fake = FakeAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    phase = loop.run(tmp_path, env)
    end = [e for e in events.read(env) if e["event"] == "run_end"][-1]
    assert end["phase"] == phase


def test_loop_emits_gate_skipped_for_off_stages(tmp_path, monkeypatch):
    env = _env_with_task(tmp_path)  # verify=false, planning=false, oracle=false
    fake = FakeAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)
    skipped = {e["stage"] for e in events.read(env)
               if e["event"] == "gate_skipped"}
    assert {"verify", "oracle"} <= skipped  # configured off → recorded


# --- pipeline graph (Phase 3) ---------------------------------------------- #

def test_active_stages_respects_gates():
    from harn.config import Config
    on = Config(verify=True, oracle=True, browser_enabled=True, planning=True)
    names = {s.name for s in loop.active_stages(on)}
    assert {"plan", "execute", "verify", "ui_verify", "oracle", "reconcile"} <= names

    off = Config(verify=False, oracle=False, browser_enabled=False, planning=False)
    names_off = {s.name for s in loop.active_stages(off)}
    assert "verify" not in names_off
    assert "oracle" not in names_off
    assert "ui_verify" not in names_off
    # non-optional stages always present
    assert "execute" in names_off and "reconcile" in names_off


def test_explain_marks_on_and_off():
    from harn.config import Config
    text = loop.explain(Config(verify=True, oracle=False, browser_enabled=False))
    assert "verify" in text and "oracle" in text
    # numbered, ordered, with on/off tags
    assert "1." in text and "(on)" in text and "(off)" in text
    assert "always" in text  # execute / reconcile
