"""Run-level budget guard (Phase 8, Task 2 + 2b). Task 2 added `_over_budget`,
checked only at the sequential-step `_run_turn` call site. Task 2b closes the
blind spot: `_RunSpend` is a single thread-safe accumulator threaded through
`_run_turn` (the one choke point every in-run agent turn passes through), so
wave members, on_fail handlers, and merge turns all feed the SAME guard the
sequential path does."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from harn.loop import _over_budget, _RunSpend
from harn.config import Config
from harn import loop, tasks, workflows, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


def _cfg(**kw):
    c = Config(); [setattr(c, k, v) for k, v in kw.items()]; return c


# --------------------------------------------------------------------------- #
# _over_budget (Task 2, kept green)
# --------------------------------------------------------------------------- #
def test_cost_ceiling_trips():
    r = _over_budget(3.5, 1000, _cfg(max_cost_usd=3.0, max_tokens=0))
    assert r and "3.5" in r and "3.0" in r

def test_token_ceiling_trips():
    r = _over_budget(0.1, 500_000, _cfg(max_cost_usd=0.0, max_tokens=400_000))
    assert r and "500000" in r

def test_zero_ceilings_never_trip():
    assert _over_budget(9_999.0, 99_000_000, _cfg(max_cost_usd=0.0, max_tokens=0)) == ""

def test_under_budget_is_clear():
    assert _over_budget(0.5, 1000, _cfg(max_cost_usd=3.0, max_tokens=400_000)) == ""


# --------------------------------------------------------------------------- #
# _RunSpend — thread-safety (Task 2b)
# --------------------------------------------------------------------------- #
def test_run_spend_accumulates_under_concurrent_writers():
    """8 threads x 1000 adds each, no lost updates thanks to the lock."""
    spend = _RunSpend()
    n_threads, n_adds = 8, 1000

    def worker():
        for _ in range(n_adds):
            spend.add(0.01, 100)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert spend.cost == pytest.approx(0.01 * n_threads * n_adds)
    assert spend.tok == 100 * n_threads * n_adds


def test_run_spend_add_tolerates_none():
    spend = _RunSpend()
    spend.add(None, None)   # must not raise
    assert spend.cost == 0.0
    assert spend.tok == 0
    spend.add(1.5, None)
    spend.add(None, 50)
    assert spend.cost == pytest.approx(1.5)
    assert spend.tok == 50


def test_run_spend_excludes_cache_read_tokens():
    # Regression: a normal $0.12 Claude turn reports ~540k tokens, ~410k of
    # them cache reads (cheap context re-reads). Counting those made the token
    # budget trip on the FIRST turn of every task. The budget must count only
    # non-cache tokens (fresh input + cache creation + output).
    spend = _RunSpend()
    spend.add(0.12, 541626, 410000)      # total 541626, of which 410000 cache reads
    assert spend.cost == pytest.approx(0.12)
    assert spend.tok == 541626 - 410000  # 131626 counted, not 541626
    # A pathological/garbage cache_read larger than total must not go negative.
    spend2 = _RunSpend()
    spend2.add(0.0, 100, 999999)
    assert spend2.tok == 0
    # cache_read defaults to 0 (older adapters that don't report it).
    spend3 = _RunSpend()
    spend3.add(0.0, 100)
    assert spend3.tok == 100


def test_run_turn_spend_none_is_a_no_op():
    """The run_step path calls _run_turn without `spend` at all — confirm the
    default is None and behavior (return value) is identical either way."""
    import inspect
    assert inspect.signature(loop._run_turn).parameters["spend"].default is None


# --------------------------------------------------------------------------- #
# on_fail path feeds the accumulator (Task 2b's actual blind spot) — driven
# end-to-end through loop.run() so the wiring from run()'s command-step call
# site through _run_command_step -> _run_onfail_handler -> _run_turn is
# exercised for real, not just asserted structurally.
# --------------------------------------------------------------------------- #
class _CostlyAdapter:
    """Every call reports a fixed cost/token usage, so N on_fail dispatches
    burn N * usage — enough to trip a tight budget."""
    name = "fake"
    def __init__(self, cost_usd=2.0, total_tokens=1000):
        self.calls = []
        self._cost = cost_usd
        self._tokens = total_tokens
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        return AgentResult(ok=True, text="still broken",
                           input_tokens=self._tokens, output_tokens=0,
                           cost_usd=self._cost)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _step(title, **kw):
    base = {"kind": "step", "title": title, "body": "", "id": "", "agent": "",
            "model": "", "effort": "", "temperature": "", "type": "",
            "command": "", "on_fail": "", "required": [], "tools": [],
            "enabled": True}
    base.update(kw)
    return base


def _project(tmp_path, nodes, max_cost_usd=0.0, max_tokens=0):
    """Same shape as test_command_steps.py's fixture, but with the budget
    ceilings exposed (Config defaults them to $3.00/400000 tokens — off, for
    these tests, means passing 0 explicitly, not omitting the keys)."""
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        f"[loop]\nmax_iterations = 20\nmax_cost_usd = {max_cost_usd}\n"
        f"max_tokens = {max_tokens}\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    workflows.save_task_plan(env, t.id, {"preamble": "", "nodes": nodes})
    return env, t


def test_onfail_handler_spend_trips_the_run_budget(tmp_path, monkeypatch):
    """A command step that keeps failing dispatches its on_fail handler every
    iteration. Task 2's OLD guard only counted sequential-step turns, so this
    on_fail-driven spend was invisible to it and the run would burn past the
    cap. With `spend` threaded into `_run_command_step` ->
    `_run_onfail_handler` -> `_run_turn`, the SAME accumulator the sequential
    path uses now sees these turns too, and the run BLOCKS."""
    env, t = _project(
        tmp_path,
        [
            _step("Tests", id="step-t1", type="command", command="false",
                  on_fail="step-fix"),
            _step("Fix tests", id="step-fix"),
        ],
        max_cost_usd=5.0, max_tokens=0,
    )
    fake = _CostlyAdapter(cost_usd=2.0, total_tokens=1000)
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    from harn import state
    phase = loop.run(tmp_path, env)

    assert phase == state.BLOCKED
    # 3 handler dispatches x $2.00 = $6.00 >= $5.00 cap — but it never even
    # gets there if it isn't blocked well before max_iterations=20 exhausts.
    assert len(fake.calls) <= 3
    assert (env / "state" / "BLOCKED.md").exists()
    assert "budget" in (env / "state" / "BLOCKED.md").read_text().lower()


def test_onfail_handler_without_budget_config_runs_to_iteration_limit(tmp_path, monkeypatch):
    """Sanity control: with no budget configured (Task 1's 0 = off default),
    the SAME on_fail-looping scenario is bounded only by max_iterations, same
    as before Task 2b — proving the new checkpoint doesn't fire spuriously."""
    env, t = _project(
        tmp_path,
        [
            _step("Tests", id="step-t1", type="command", command="false",
                  on_fail="step-fix"),
            _step("Fix tests", id="step-fix"),
        ],
        max_cost_usd=0.0, max_tokens=0,
    )
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 5\nmax_cost_usd = 0\nmax_tokens = 0\n"
        "[notify]\nwait_for_reply = false\n")
    fake = _CostlyAdapter(cost_usd=2.0, total_tokens=1000)
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)

    assert not (env / "state" / "BLOCKED.md").exists()
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "failed"


# --------------------------------------------------------------------------- #
# Wave / merge coverage — wiring-verified, not integration-tested. A true
# wave-overspend end-to-end test needs real git worktrees + concurrent agent
# turns tripping the guard mid-fan-out; that's heavy scaffolding disproportionate
# to what's being proven here (the choke point + lock are already covered
# above and by test_run_spend_accumulates_under_concurrent_writers). Instead,
# assert directly that `spend` is accepted and forwarded at every remaining
# call site, so a future signature change that silently drops it fails loudly.
# --------------------------------------------------------------------------- #
def test_wave_and_merge_helpers_accept_and_forward_spend():
    import inspect

    for fn in (loop._run_parallel_wave, loop._merge_wave_patches,
               loop._run_merge_agent_turn, loop._run_command_step,
               loop._run_onfail_handler):
        params = inspect.signature(fn).parameters
        assert "spend" in params, f"{fn.__name__} lost its spend param"
        assert params["spend"].default is None

    src = inspect.getsource(loop._run_parallel_wave)
    assert "spend=spend" in src   # _run_one's _run_turn call + the merge call
    src = inspect.getsource(loop._merge_wave_patches)
    assert "spend=spend" in src
    src = inspect.getsource(loop._run_merge_agent_turn)
    assert "spend=spend" in src
    src = inspect.getsource(loop._run_command_step)
    assert "spend=spend" in src
    src = inspect.getsource(loop._run_onfail_handler)
    assert "spend=spend" in src


# --------------------------------------------------------------------------- #
# Ordering bug (Phase 8, Task 2b follow-up): an on_fail-handler turn that BOTH
# raises a real question (ask_user -> BLOCKED) AND tips the run over budget
# must have the blocked outcome win. The command-step checkpoint in run()
# used to check the budget BEFORE handling `outcome == "blocked"`, so
# _block_on_budget would overwrite the just-persisted real question (using a
# stale outer `st`) with the budget message. Mirrors the wave checkpoint,
# which already gets this ordering right.
# --------------------------------------------------------------------------- #
class _BlockingCostlyAdapter(_CostlyAdapter):
    """Like _CostlyAdapter, but also raises a real ask_user-style question
    (by writing BLOCKED.md, the same cross-agent signal `ask_user` uses) on
    its first call — simulating an on_fail handler turn that both blocks AND
    burns enough spend to trip the budget guard in the same turn."""
    REAL_QUESTION = "Which retry strategy should the fix use — exponential or fixed backoff?"

    def __init__(self, state_dir: Path, cost_usd=10.0, total_tokens=1000):
        super().__init__(cost_usd=cost_usd, total_tokens=total_tokens)
        self._state_dir = state_dir

    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self._state_dir.mkdir(parents=True, exist_ok=True)
        (self._state_dir / "BLOCKED.md").write_text(self.REAL_QUESTION)
        return super().run_turn(prompt, cwd, timeout=timeout, model=model,
                                effort=effort, temperature=temperature)


def test_onfail_block_and_overspend_in_same_turn_keeps_the_real_question(
    tmp_path, monkeypatch
):
    """Regression for the state-ordering bug: budget must not clobber a real
    BLOCKED question. A single on_fail-handler turn both calls ask_user
    (BLOCKED.md written) and reports enough cost to blow past a $1.00 cap
    (cost_usd=10.0). If the budget check ran first (the bug), STATE.json's
    `question` and BLOCKED.md would hold the generic budget-ceiling message
    instead of REAL_QUESTION, and the persisted reason for the stop would be
    wrong even though the run correctly still stops."""
    env, t = _project(
        tmp_path,
        [
            _step("Tests", id="step-t1", type="command", command="false",
                  on_fail="step-fix"),
            _step("Fix tests", id="step-fix"),
        ],
        max_cost_usd=1.0, max_tokens=0,
    )
    state_dir = env / "state"
    fake = _BlockingCostlyAdapter(state_dir, cost_usd=10.0, total_tokens=1000)
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    from harn import state
    phase = loop.run(tmp_path, env)

    assert phase == state.BLOCKED
    # Exactly one on_fail dispatch — the block must stop the run before a
    # second (budget-driven) iteration could even start.
    assert len(fake.calls) == 1

    st = state.State.load(state_dir)
    assert st.question == _BlockingCostlyAdapter.REAL_QUESTION
    assert "budget" not in (st.question or "").lower()

    blocked_text = (env / "state" / "BLOCKED.md").read_text()
    assert blocked_text.strip() == _BlockingCostlyAdapter.REAL_QUESTION
    assert "budget" not in blocked_text.lower()


def test_run_wires_a_single_shared_spend_into_all_three_checkpoints():
    """run()'s source must create exactly one _RunSpend and pass it to the
    sequential turn, the command-step path, and the parallel-wave path —
    proving all three checkpoints share ONE accumulator, not three separate
    (and separately under-counting) ones."""
    import inspect
    src = inspect.getsource(loop.run)
    assert src.count("_RunSpend()") == 1
    assert "spend=spend" in src
    assert src.count("_over_budget(spend.cost, spend.tok, cfg)") == 3
