from pathlib import Path
from unittest.mock import MagicMock
from harn import studio, tools as tools_mod


def test_stale_when_disk_tool_missing_from_live(monkeypatch, tmp_path):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    tools_mod.save(env, "run_lint", "d", ["path"], "eslint {path}", source="t")
    # Live server reports NO custom tools (simulates a stale server).
    monkeypatch.setattr(studio.mcp_server, "healthcheck",
                        lambda e, timeout=40: (True, ["read_skill", "board"], ""))
    p = studio.mcp_health_payload(env)
    assert p["running"] is True
    assert "run_lint" in p["disk_custom_names"]
    assert "run_lint" not in p["custom_names"]
    assert p["stale"] is True

def test_not_stale_when_live_has_the_tool(monkeypatch, tmp_path):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    tools_mod.save(env, "run_lint", "d", ["path"], "eslint {path}", source="t")
    monkeypatch.setattr(studio.mcp_server, "healthcheck",
                        lambda e, timeout=40: (True, ["read_skill", "run_lint"], ""))
    p = studio.mcp_health_payload(env)
    assert p["stale"] is False
    assert p["tools_count"] == 2

def test_down_when_healthcheck_fails(monkeypatch, tmp_path):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setattr(studio.mcp_server, "healthcheck",
                        lambda e, timeout=40: (False, [], "boom"))
    p = studio.mcp_health_payload(env, force=True)  # bypass any stale cache
    assert p["running"] is False
    assert p["error"] == "boom"
    assert p["stale"] is False  # can't be stale if it isn't running


def test_health_probe_is_cached_so_rapid_polls_dont_spawn_subprocesses(monkeypatch, tmp_path):
    # Regression: healthcheck() spawns a full `harn mcp` subprocess; the badge
    # polled it every ~1.5s, and without caching that continuous subprocess
    # churn pinned a core and made the whole studio UI laggy (dropped
    # keystrokes in every input). The probe must run at most once per TTL.
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    studio._mcp_health_cache.clear()
    calls = {"n": 0}
    def fake_hc(e, timeout=40):
        calls["n"] += 1
        return (True, ["board"], "")
    monkeypatch.setattr(studio.mcp_server, "healthcheck", fake_hc)

    studio.mcp_health_payload(env)          # 1 real probe
    studio.mcp_health_payload(env)          # cached
    studio.mcp_health_payload(env)          # cached
    assert calls["n"] == 1                  # only ONE subprocess, not three

    studio.mcp_health_payload(env, force=True)   # explicit bypass (e.g. restart)
    assert calls["n"] == 2


def _make_supervisor(tmp_path):
    env = tmp_path / "harn_env"
    (env / "state").mkdir(parents=True)
    sup = studio._MCPSupervisor(env, 9123)
    return sup


def test_maybe_respawn_noop_after_final_stop(monkeypatch, tmp_path):
    """Once stop(_final=True) has run, the monitor's respawn decision must
    be a no-op even if it observes a dead proc — this is the shutdown race
    (leak #2): a monitor tick can't sneak a spawn in after stop() returns."""
    sup = _make_supervisor(tmp_path)
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1  # dead
    sup.proc = dead_proc

    spawn_mock = MagicMock()
    monkeypatch.setattr(sup, "_spawn_locked", spawn_mock)

    sup.stop(_final=True)
    assert sup._stop.is_set()

    sup._maybe_respawn()
    spawn_mock.assert_not_called()


def test_restart_after_final_stop_never_spawns(monkeypatch, tmp_path):
    """An HTTP-triggered restart() racing serve()'s Ctrl-C shutdown must NOT
    spawn a new child once stop(_final=True) has run — otherwise it orphans a
    process the finally-block stop() will never reap. restart() returns False
    without spawning in that case."""
    sup = _make_supervisor(tmp_path)
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1  # dead
    sup.proc = dead_proc

    spawn_mock = MagicMock()
    monkeypatch.setattr(sup, "_spawn_locked", spawn_mock)

    sup.stop(_final=True)
    assert sup.restart() is False
    spawn_mock.assert_not_called()


def test_restart_then_maybe_respawn_only_one_spawn(monkeypatch, tmp_path):
    """restart() and a concurrent monitor _maybe_respawn() must not both
    spawn — this is the restart race (leak #1). After restart() finishes,
    the proc is alive, so a subsequent _maybe_respawn() call sees a live
    proc and does nothing further."""
    sup = _make_supervisor(tmp_path)

    live_proc = MagicMock()
    live_proc.poll.return_value = None  # alive

    def fake_spawn_locked():
        sup.proc = live_proc

    spawn_mock = MagicMock(side_effect=fake_spawn_locked)
    monkeypatch.setattr(sup, "_spawn_locked", spawn_mock)

    # Simulate a dead proc that triggers restart().
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1
    dead_proc.wait.return_value = None
    sup.proc = dead_proc

    sup.restart()
    assert spawn_mock.call_count == 1

    # A monitor tick landing right after restart() sees a live proc — no
    # additional spawn.
    sup._maybe_respawn()
    assert spawn_mock.call_count == 1


def test_maybe_respawn_spawns_when_dead_and_not_stopped(monkeypatch, tmp_path):
    """Sanity check for the gating logic itself: when the proc is dead and
    stop() has not been called, _maybe_respawn() does spawn."""
    sup = _make_supervisor(tmp_path)
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1
    sup.proc = dead_proc

    spawn_mock = MagicMock()
    monkeypatch.setattr(sup, "_spawn_locked", spawn_mock)

    sup._maybe_respawn()
    spawn_mock.assert_called_once()


def test_maybe_respawn_respects_restart_budget(monkeypatch, tmp_path):
    """Bounded restarts (<=5/60s) must still be enforced through the new
    lock-guarded gating path."""
    sup = _make_supervisor(tmp_path)
    sup._restarts = [__import__("time").time()] * 5  # budget exhausted

    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1
    sup.proc = dead_proc

    spawn_mock = MagicMock()
    monkeypatch.setattr(sup, "_spawn_locked", spawn_mock)

    sup._maybe_respawn()
    spawn_mock.assert_not_called()
