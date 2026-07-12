# UI-supervised MCP + hot-reload + loop budget — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make custom tools always reachable (hot-reload + UI-supervised MCP with health) and make a run unable to burn unbounded tokens (cost+token budget → BLOCKED), with every new knob editable/disableable in the studio Settings tab.

**Architecture:** Three independent-ish slices on one branch. (A) A daemon watcher inside `mcp_server.build_server()` reconciles the live `ToolManager` against `harn_env/tools/` so any running MCP self-heals. (B) `studio.serve()` supervises an owned `harn mcp --http` child (pid file, bounded restart) and exposes health + restart routes and a header badge. (C) `loop.run()` sums per-turn spend and stops → BLOCKED when a cost or token ceiling is crossed. All knobs live in `[loop]`/`[mcp]` config and the Settings tab.

**Tech Stack:** Python stdlib + the already-present `mcp` SDK (FastMCP `mcp.server.fastmcp`, `ToolManager.add_tool/remove_tool`); studio is stdlib `http.server` + vanilla JS.

## Global Constraints

- Bump `harn/__init__.py` `__version__` AND `pyproject.toml` `version` on **every** committed change under `harn/` (standing rule) — current version is `0.17.49`, so the first task lands `0.17.50` and each subsequent task increments the patch.
- Pure stdlib only (no new dependencies); the sole external dep is the existing `mcp` SDK.
- Reuse existing patterns verbatim: targeted-regex TOML writes (`save_defaults`/`set_config_flag`); the `_ensure_watch_running` daemon idiom for the watcher; the `runner` subprocess-with-pidfile idiom for supervision; `tools_mod.is_safe_param_name` re-validation before any `exec()`-synthesized tool function.
- **Robustness ("can't break accidentally"):** every new knob coerces to a safe value (`max(0, int(...))`, blank → 0 = off); `0` disables that guard cleanly; the watcher and the supervisor threads are daemon and never raise into a request or a turn; the budget check tolerates `None` cost/token fields (`or 0`); malformed config never crashes `Config.load` (defaults win).
- Custom-tool defaults: `[loop] max_cost_usd = 3.0`, `[loop] max_tokens = 400000`, `[loop] turn_timeout_seconds = 1800`; `[mcp] ui_supervise = true`, `[mcp] ui_port = 8765`, `[mcp] tool_reload_seconds = 2`.

---

## File Structure

- `harn/config.py` — add six config fields + defaults + parsing (Task 1).
- `harn/loop.py` — budget enforcement + turn timeout wiring in `run()` (Task 2).
- `harn/mcp_server.py` — `_ToolReloader` watcher + `start_tool_reload()` wired into `build_server()` (Task 3).
- `harn/studio.py` — MCP supervisor (`_MCPSupervisor`), `mcp_health_payload`, `restart_mcp_payload`, `save_loop_mcp_settings`, `settings_payload`, routes, and all UI (Settings sections, header badge, run-banner spend) (Tasks 4–6).
- `tests/test_config_phase8.py`, `tests/test_loop_budget.py`, `tests/test_tool_reload.py`, `tests/test_mcp_supervisor.py`, `tests/test_settings_save.py` — new test modules (Tasks 1–5).
- `harn/templates/harn.toml`, `docs/` — documented knobs (Task 7).

---

## Task 1: Config — six new knobs, safely parsed

**Files:**
- Modify: `harn/config.py` (DEFAULTS dict ~line 85 `"loop"` + new `"mcp"` keys ~line 94; `Config` dataclass fields ~line 121; `Config.load` return ~line 195)
- Modify: `harn/__init__.py`, `pyproject.toml` (version bump)
- Test: `tests/test_config_phase8.py`

**Interfaces:**
- Produces: `Config` gains fields `max_cost_usd: float = 3.0`, `max_tokens: int = 400000`, `turn_timeout_seconds: int = 1800`, `mcp_ui_supervise: bool = True`, `mcp_ui_port: int = 8765`, `mcp_tool_reload_seconds: int = 2`. All read from `[loop]` (first three) and `[mcp]` (last three). Non-negative coercion: negatives clamp to 0.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_phase8.py
from pathlib import Path
from harn.config import Config

def _write(tmp_path: Path, toml: str) -> Path:
    env = tmp_path / "harn_env"; env.mkdir()
    (env / "harn.toml").write_text(toml, encoding="utf-8")
    return env

def test_budget_defaults_when_absent(tmp_path):
    cfg = Config.load(_write(tmp_path, "[harn]\nagent = \"claude\"\n"))
    assert cfg.max_cost_usd == 3.0
    assert cfg.max_tokens == 400000
    assert cfg.turn_timeout_seconds == 1800
    assert cfg.mcp_ui_supervise is True
    assert cfg.mcp_ui_port == 8765
    assert cfg.mcp_tool_reload_seconds == 2

def test_budget_overrides_and_zero_disables(tmp_path):
    cfg = Config.load(_write(tmp_path,
        "[loop]\nmax_cost_usd = 0\nmax_tokens = 0\nturn_timeout_seconds = 0\n"
        "[mcp]\nui_supervise = false\nui_port = 9100\ntool_reload_seconds = 0\n"))
    assert cfg.max_cost_usd == 0.0
    assert cfg.max_tokens == 0
    assert cfg.turn_timeout_seconds == 0
    assert cfg.mcp_ui_supervise is False
    assert cfg.mcp_ui_port == 9100
    assert cfg.mcp_tool_reload_seconds == 0

def test_negative_values_clamp_to_zero(tmp_path):
    cfg = Config.load(_write(tmp_path,
        "[loop]\nmax_cost_usd = -5\nmax_tokens = -1\n"))
    assert cfg.max_cost_usd == 0.0
    assert cfg.max_tokens == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config_phase8.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'max_cost_usd'`

- [ ] **Step 3: Implement**

In `harn/config.py` DEFAULTS, extend the `"loop"` dict and the `"mcp"` dict:

```python
    "loop": {"max_iterations": 10, "loop_aware": True,
             "auto": False, "auto_max_iterations": 30,
             "oracle": True, "oracle_agent": "",
             "design": True, "auto_reconcile": True,
             "max_cost_usd": 3.0, "max_tokens": 400000,
             "turn_timeout_seconds": 1800},
    ...
    "mcp": {"context7": True, "ui_supervise": True, "ui_port": 8765,
            "tool_reload_seconds": 2},
```

Add dataclass fields (near `max_iterations`):

```python
    # Run-level spend ceilings (0 = unlimited). Checked between turns in
    # loop.run(): crossing either stops the run and BLOCKS, so a single
    # runaway turn can't quietly burn the budget.
    max_cost_usd: float = 3.0
    max_tokens: int = 400000
    # Per-turn subprocess timeout handed to adapter.run_turn (0 = the
    # adapter's own 1800s default). Caps a single turn's blast radius.
    turn_timeout_seconds: int = 1800
    # harn ui supervises an owned `harn mcp --http` child on this port.
    mcp_ui_supervise: bool = True
    mcp_ui_port: int = 8765
    # Hot-reload: the MCP watcher polls harn_env/tools/ this often (0 = off).
    mcp_tool_reload_seconds: int = 2
```

Add a small clamp helper near `_clamp01` (top of file):

```python
def _nonneg_float(v) -> float:
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0

def _nonneg_int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0
```

In `Config.load`'s `return cls(...)`, add:

```python
            max_cost_usd=_nonneg_float(data["loop"].get("max_cost_usd", 3.0)),
            max_tokens=_nonneg_int(data["loop"].get("max_tokens", 400000)),
            turn_timeout_seconds=_nonneg_int(data["loop"].get("turn_timeout_seconds", 1800)),
            mcp_ui_supervise=bool(data["mcp"].get("ui_supervise", True)),
            mcp_ui_port=_nonneg_int(data["mcp"].get("ui_port", 8765)),
            mcp_tool_reload_seconds=_nonneg_int(data["mcp"].get("tool_reload_seconds", 2)),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config_phase8.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Bump version + commit**

Set `harn/__init__.py` `__version__ = "0.17.50"` and `pyproject.toml` `version = "0.17.50"`.

```bash
git add harn/config.py harn/__init__.py pyproject.toml tests/test_config_phase8.py
git commit -m "feat(config): loop budget + mcp supervision/reload knobs (Phase 8); version 0.17.50"
```

---

## Task 2: Loop budget enforcement + per-turn timeout

**Files:**
- Modify: `harn/loop.py` — `run()` (turn call ~line 1878; add run-level accumulators near ~line 1702) and `_run_turn` (~line 772, thread a `timeout` kwarg through to `adapter.run_turn`)
- Modify: `harn/__init__.py`, `pyproject.toml` (version bump)
- Test: `tests/test_loop_budget.py`

**Interfaces:**
- Consumes: `Config.max_cost_usd`, `Config.max_tokens`, `Config.turn_timeout_seconds` (Task 1).
- Produces: a private helper `_over_budget(run_cost: float, run_tok: int, cfg) -> str` returning a non-empty human reason string when a ceiling is crossed, else `""`. Used only inside `run()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_loop_budget.py
from harn.loop import _over_budget
from harn.config import Config

def _cfg(**kw):
    c = Config(); [setattr(c, k, v) for k, v in kw.items()]; return c

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_loop_budget.py -v`
Expected: FAIL — `ImportError: cannot import name '_over_budget'`

- [ ] **Step 3: Implement the helper + wire it into `run()`**

Add near the other `run()` helpers in `harn/loop.py` (module scope, above `def run`):

```python
def _over_budget(run_cost: float, run_tok: int, cfg) -> str:
    """A run-level spend guard checked between turns. Returns a human reason
    when a configured ceiling is crossed, else "". 0 = that ceiling is off.
    Tolerant of odd inputs — never raises into the loop."""
    cost_cap = getattr(cfg, "max_cost_usd", 0.0) or 0.0
    tok_cap = getattr(cfg, "max_tokens", 0) or 0
    if cost_cap and run_cost >= cost_cap:
        return (f"Run stopped: budget exceeded — spent ${run_cost:.2f}, "
                f"cap ${cost_cap:.2f}. Raise the budget in Settings or split "
                f"the task, then Resume.")
    if tok_cap and run_tok >= tok_cap:
        return (f"Run stopped: budget exceeded — used {run_tok} tokens, "
                f"cap {tok_cap}. Raise the budget in Settings or split the "
                f"task, then Resume.")
    return ""
```

In `run()`, declare run-level accumulators alongside `tok_totals`/`tok_costs` (~line 1702):

```python
    run_cost_total = 0.0     # cumulative across the WHOLE run (never popped)
    run_tok_total = 0        # — the budget guard's source of truth
```

Immediately after the sequential-step turn returns (`result = _run_turn(...)`, ~line 1883) and `st.current_step` is cleared, add the accumulate + guard, BEFORE the block/feedback handling:

```python
            run_cost_total += (result.cost_usd or 0.0)
            run_tok_total += (result.total_tokens or 0)
            over = _over_budget(run_cost_total, run_tok_total, cfg)
            if over:
                state.blocked_marker(state_dir).write_text(over, encoding="utf-8")
                st.block(over)
                st.save(state_dir)
                events.emit(env_dir, "block", task_id=task.id, stage=sid,
                            detail=over[:300])
                if not auto:
                    task.step_results[sid] = {**task.step_results.get(sid, {}),
                                              "status": "blocked"}
                    tasks._save(task)
                    progress.log(env_dir, f"{task.id}: {over}", agent=step_adapter.name)
                print(f"[harn] {task.id}: {over}")
                return _run_end(env_dir, st)
```

Wire the per-turn timeout: give `_run_turn` a `timeout` param and pass it to the adapter. Change the signature (~line 772) to add `timeout: int | None = None` and the call inside (`res = adapter.run_turn(prompt, project_root, **(overrides or {}))`) to:

```python
        call_kw = dict(overrides or {})
        if timeout:               # 0 / None → adapter's own 1800s default
            call_kw["timeout"] = timeout
        res = adapter.run_turn(prompt, project_root, **call_kw)
```

At the sequential-step `_run_turn(...)` call in `run()`, pass `timeout=cfg.turn_timeout_seconds`. (The `total_tokens` property already exists on `AgentResult`; if unsure, confirm with `grep -n "total_tokens" harn/adapters/base.py` — it sums input+output.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_loop_budget.py tests/ -q`
Expected: PASS — new file green, full suite still green.

- [ ] **Step 5: Bump version + commit**

`0.17.51`.

```bash
git add harn/loop.py harn/__init__.py pyproject.toml tests/test_loop_budget.py
git commit -m "feat(loop): cost+token run budget → BLOCKED; per-turn timeout knob (Phase 8); version 0.17.51"
```

---

## Task 3: Hot-reload custom tools in the running MCP server

**Files:**
- Modify: `harn/mcp_server.py` — add `_tools_dir_signature`, `_reconcile_custom_tools`, `start_tool_reload`; call `start_tool_reload(mcp)` inside `build_server()` when `register_custom` and `tool_reload_seconds > 0`
- Modify: `harn/__init__.py`, `pyproject.toml` (version bump)
- Test: `tests/test_tool_reload.py`

**Interfaces:**
- Consumes: `tools_mod.discover`, `tools_mod.is_safe_param_name`, `_make_tool_function`, `_record_tool_used` (all in `mcp_server.py`); `Config.mcp_tool_reload_seconds`.
- Produces: `_reconcile_custom_tools(mcp, env_dir, registered: set[str]) -> set[str]` — reconciles the live `ToolManager` to match the safe custom tools on disk; returns the new set of registered custom-tool names. Pure enough to unit-test with one tick (no thread).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_reload.py
import asyncio
from pathlib import Path
from harn import mcp_server, tools as tools_mod

def _tool(env: Path, name: str, params, command):
    tools_mod.save(env, name, "desc", params, command, source="test")

def _names(mcp):
    return {t.name for t in asyncio.run(mcp.list_tools())}

def test_reconcile_adds_new_tool(tmp_path, monkeypatch):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    mcp = mcp_server.build_server(start_watch=False, register_custom=True)
    assert "run_lint" not in _names(mcp)
    _tool(env, "run_lint", ["path"], "eslint {path}")
    reg = mcp_server._reconcile_custom_tools(mcp, env, set())
    assert "run_lint" in reg
    assert "run_lint" in _names(mcp)

def test_reconcile_removes_deleted_tool(tmp_path, monkeypatch):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    _tool(env, "run_lint", ["path"], "eslint {path}")
    mcp = mcp_server.build_server(start_watch=False, register_custom=True)
    (env / "tools" / "run_lint.json").unlink()
    reg = mcp_server._reconcile_custom_tools(mcp, env, {"run_lint"})
    assert "run_lint" not in reg
    assert "run_lint" not in _names(mcp)

def test_reconcile_skips_unsafe_param(tmp_path, monkeypatch):
    env = tmp_path / "harn_env"; (env / "tools").mkdir(parents=True)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    # Bypass tools_mod.save's gate: write a malicious json straight to disk.
    (env / "tools" / "evil.json").write_text(
        '{"name":"evil","description":"x","params":["a; rm -rf /"],'
        '"command":"echo {a}","source":"x"}', encoding="utf-8")
    mcp = mcp_server.build_server(start_watch=False, register_custom=True)
    reg = mcp_server._reconcile_custom_tools(mcp, env, set())
    assert "evil" not in reg
    assert "evil" not in _names(mcp)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tool_reload.py -v`
Expected: FAIL — `AttributeError: module 'harn.mcp_server' has no attribute '_reconcile_custom_tools'`

- [ ] **Step 3: Implement**

In `harn/mcp_server.py`, add (near the `build_server` custom-tool loop):

```python
import threading

def _tools_dir_signature(env_dir: Path) -> tuple:
    """Cheap change-detector for harn_env/tools/: (name, mtime, size) per json,
    sorted. Never raises — a stat error yields an empty signature (treated as
    'no tools'), which the reconciler handles safely."""
    d = env_dir / "tools"
    try:
        out = []
        for p in sorted(d.glob("*.json")):
            st = p.stat()
            out.append((p.name, st.st_mtime, st.st_size))
        return tuple(out)
    except Exception:
        return ()

def _reconcile_custom_tools(mcp, env_dir: Path, registered: set) -> set:
    """Make the live ToolManager match the SAFE custom tools on disk. Adds
    new/changed, removes deleted, skips unsafe-param tools (same gate as the
    boot loop). Returns the new registered-name set. Best-effort, never raises."""
    tm = mcp._tool_manager
    try:
        discovered = {t.name: t for t in tools_mod.discover(env_dir)}
    except Exception as exc:
        _log(f"tool reload: discover failed ({exc}); keeping current set")
        return registered
    safe = {}
    for name, ct in discovered.items():
        unsafe = [p for p in ct.params if not tools_mod.is_safe_param_name(p)]
        if unsafe:
            _log(f"tool reload: '{name}' skipped: unsafe param(s) {unsafe!r}")
            continue
        safe[name] = ct
    changed = False
    # Remove tools that vanished or became unsafe.
    for name in list(registered):
        if name not in safe:
            try:
                tm.remove_tool(name)
            except Exception:
                pass
            changed = True
    # Add/replace current safe tools (always re-add so an edited command/params
    # takes effect — remove-then-add makes it idempotent).
    new_reg = set()
    for name, ct in safe.items():
        try:
            if name in {t.name for t in tm.list_tools()}:
                tm.remove_tool(name)
            fn = _make_tool_function(ct, env_dir.parent, _record_tool_used)
            mcp.add_tool(fn, name=ct.name, description=ct.description)
            new_reg.add(name)
            changed = True
        except Exception as exc:
            _log(f"tool reload: '{name}' failed to register ({exc})")
    if changed:
        _notify_tools_changed(mcp)
    return new_reg

def _notify_tools_changed(mcp) -> None:
    """Best-effort: tell connected clients the tool list changed. If no active
    session is reachable from this thread, the ToolManager is still correct so
    the next tools/list is fresh anyway. Never raises."""
    try:
        session = mcp._mcp_server.request_context.session
        import anyio
        anyio.from_thread.run(session.send_tool_list_changed)
    except Exception:
        pass

def start_tool_reload(mcp, interval_s: int) -> None:
    """Daemon watcher: poll harn_env/tools/ every interval_s and reconcile the
    live server. interval_s <= 0 disables it. Never blocks server startup."""
    if interval_s <= 0:
        return
    env_dir = _env_dir()
    def _loop():
        registered = {t.name for t in mcp._tool_manager.list_tools()
                      if t.name in {c.name for c in tools_mod.discover(env_dir)}}
        last = _tools_dir_signature(env_dir)
        while True:
            time.sleep(interval_s)
            sig = _tools_dir_signature(env_dir)
            if sig != last:
                last = sig
                registered = _reconcile_custom_tools(mcp, env_dir, registered)
    threading.Thread(target=_loop, name="harn-tool-reload", daemon=True).start()
```

Add `import time` if not already imported. In `build_server()`, after the `register_custom` boot loop (right before `return mcp`):

```python
    if register_custom:
        try:
            interval = Config.load(_env_dir()).mcp_tool_reload_seconds
        except Exception:
            interval = 2
        start_tool_reload(mcp, interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_tool_reload.py tests/ -q`
Expected: PASS — 3 new tests green, full suite green.

- [ ] **Step 5: Bump version + commit**

`0.17.52`.

```bash
git add harn/mcp_server.py harn/__init__.py pyproject.toml tests/test_tool_reload.py
git commit -m "feat(mcp): hot-reload custom tools in a running server (Phase 8); version 0.17.52"
```

---

## Task 4: studio — supervise an MCP child + health/restart routes

**Files:**
- Modify: `harn/studio.py` — add module-top imports; `_MCPSupervisor` class + module singleton; `mcp_health_payload(env_dir)`; `restart_mcp_payload(env_dir)`; start/stop hooks in `serve()`; GET `/api/mcp/health` and POST `/api/mcp/restart` routes
- Modify: `harn/__init__.py`, `pyproject.toml` (version bump)
- Test: `tests/test_mcp_supervisor.py`

**Required imports (add to studio.py module top):** `import os`, `import subprocess`, `import sys`, `import time`, and `from . import mcp_server` (currently `mcp_server` is imported lazily *inside* functions — the test does `monkeypatch.setattr(studio.mcp_server, "healthcheck", …)`, which needs `studio.mcp_server` resolvable at module scope; `threading` is already imported). After adding the top-level `from . import mcp_server`, the existing lazy `from . import mcp_server` lines inside functions are harmless but can be removed.

**Interfaces:**
- Consumes: `Config.mcp_ui_supervise`, `Config.mcp_ui_port`; `mcp_server.healthcheck(env_dir)`; `tools_mod.discover`.
- Produces:
  - `mcp_health_payload(env_dir) -> dict` = `{"running": bool, "port": int, "tools_count": int, "custom_names": [...], "disk_custom_names": [...], "stale": bool, "error": str, "supervised": bool}`. `stale = set(disk_custom_names) - set(custom_names) != set()` (a disk tool the live server isn't serving).
  - `restart_mcp_payload(env_dir) -> dict` = `{"ok": bool, "error": str}`.
  - Route GET `/api/mcp/health` → `mcp_health_payload`; POST `/api/mcp/restart` → `restart_mcp_payload`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_supervisor.py
from pathlib import Path
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
    p = studio.mcp_health_payload(env)
    assert p["running"] is False
    assert p["error"] == "boom"
    assert p["stale"] is False  # can't be stale if it isn't running
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_mcp_supervisor.py -v`
Expected: FAIL — `AttributeError: module 'harn.studio' has no attribute 'mcp_health_payload'`

- [ ] **Step 3: Implement**

Add to `harn/studio.py` (module scope; `mcp_server`, `tools_mod`, `Config`, `subprocess`, `sys`, `time`, `threading` are already imported or import them):

```python
def mcp_health_payload(env_dir: Path) -> dict:
    """What the studio's MCP badge needs. `running`/`tools_count` come from a
    fresh healthcheck subprocess; `stale` is true when a custom tool exists on
    disk but the live server isn't serving it — the exact PRJ-044 condition."""
    cfg = Config.load(env_dir)
    disk = sorted(t.name for t in tools_mod.discover(env_dir))
    try:
        ok, tools, err = mcp_server.healthcheck(env_dir)
    except Exception as exc:
        ok, tools, err = False, [], str(exc)
    live_custom = sorted(n for n in tools if n in set(disk))
    stale = ok and bool(set(disk) - set(live_custom))
    return {"running": bool(ok), "port": cfg.mcp_ui_port,
            "tools_count": len(tools), "custom_names": live_custom,
            "disk_custom_names": disk, "stale": stale, "error": err or "",
            "supervised": bool(cfg.mcp_ui_supervise)}


class _MCPSupervisor:
    """Owns a `harn mcp --http` child for the studio: starts it, restarts it if
    it dies (bounded to avoid hot-looping), stops it on shutdown. Best-effort —
    the studio serves fine even if the child never comes up (the badge shows
    'down')."""
    def __init__(self, env_dir: Path, port: int):
        self.env_dir = env_dir
        self.port = port
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._restarts: list[float] = []   # timestamps, rolling 60s window

    def _spawn(self) -> None:
        env = {**os.environ, "HARN_ENV_DIR": str(self.env_dir)}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "harn", "mcp", "--http",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(self.env_dir.parent), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        (self.env_dir / "state").mkdir(parents=True, exist_ok=True)
        (self.env_dir / "state" / "ui_mcp.pid").write_text(
            str(self.proc.pid), encoding="utf-8")

    def start(self) -> None:
        try:
            self._spawn()
        except Exception:
            return
        threading.Thread(target=self._monitor, name="harn-mcp-sup",
                         daemon=True).start()

    def _monitor(self) -> None:
        while not self._stop.is_set():
            time.sleep(2)
            if self._stop.is_set():
                return
            if self.proc and self.proc.poll() is not None:
                now = time.time()
                self._restarts = [t for t in self._restarts if now - t < 60]
                if len(self._restarts) >= 5:
                    continue   # too many restarts this minute — leave it down
                self._restarts.append(now)
                try:
                    self._spawn()
                except Exception:
                    pass

    def restart(self) -> None:
        self.stop(_final=False)
        self._spawn()

    def stop(self, _final: bool = True) -> None:
        if _final:
            self._stop.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        if _final:
            try:
                (self.env_dir / "state" / "ui_mcp.pid").unlink(missing_ok=True)
            except Exception:
                pass


_SUPERVISOR: _MCPSupervisor | None = None

def restart_mcp_payload(env_dir: Path) -> dict:
    if _SUPERVISOR is None:
        return {"ok": False, "error": "MCP supervision is off "
                "([mcp] ui_supervise = false)"}
    try:
        _SUPERVISOR.restart()
        return {"ok": True, "error": ""}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
```

Wire into `serve()` (start after `write`, stop in the `finally`):

```python
def serve(env_dir: Path, *, host: str = "127.0.0.1", port: int = 9999,
          open_browser: bool = True) -> None:
    global _SUPERVISOR
    workflow_mod.write(env_dir)
    cfg = Config.load(env_dir)
    if cfg.mcp_ui_supervise:
        _SUPERVISOR = _MCPSupervisor(env_dir, cfg.mcp_ui_port)
        _SUPERVISOR.start()
    httpd = ThreadingHTTPServer((host, port), _make_handler(env_dir))
    ...
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[harn] studio stopped.")
    finally:
        if _SUPERVISOR is not None:
            _SUPERVISOR.stop()
        httpd.server_close()
```

Add routes — GET (`do_GET`, near `/api/models`):

```python
            elif route == "/api/mcp/health":
                self._json(mcp_health_payload(env))
```

POST (`do_POST`, near `/api/defaults`):

```python
            elif route == "/api/mcp/restart":
                self._json(restart_mcp_payload(env))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_mcp_supervisor.py tests/ -q`
Expected: PASS — 3 new tests green, full suite green.

- [ ] **Step 5: Bump version + commit**

`0.17.53`.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml tests/test_mcp_supervisor.py
git commit -m "feat(studio): supervise an owned MCP child + health/restart routes (Phase 8); version 0.17.53"
```

---

## Task 5: studio — settings save/read backend for the new knobs

**Files:**
- Modify: `harn/studio.py` — `settings_payload(env_dir)`, `save_loop_mcp_settings(env_dir, payload)`, routes GET `/api/settings` + POST `/api/settings`
- Modify: `harn/__init__.py`, `pyproject.toml` (version bump)
- Test: `tests/test_settings_save.py`

**Interfaces:**
- Consumes: `Config.load`; existing `_toml_escape` and the targeted-regex TOML-edit idiom in `save_defaults`/`set_config_flag`.
- Produces:
  - `settings_payload(env_dir) -> dict` = `{"max_cost_usd": float, "max_tokens": int, "turn_timeout_seconds": int, "max_iterations": int, "mcp_ui_supervise": bool, "mcp_ui_port": int, "mcp_tool_reload_seconds": int}` (current values, defaults filled).
  - `save_loop_mcp_settings(env_dir, payload) -> dict` = `{"ok": bool, "error": str, **saved}`. Writes `[loop]`/`[mcp]` keys via targeted regex; rejects negatives; blank → 0; unknown keys ignored.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_settings_save.py
from pathlib import Path
from harn import studio
from harn.config import Config

def _env(tmp_path):
    env = tmp_path / "harn_env"; env.mkdir(); return env

def test_roundtrip_writes_and_reads(tmp_path):
    env = _env(tmp_path)
    r = studio.save_loop_mcp_settings(env, {
        "max_cost_usd": 5.5, "max_tokens": 250000, "turn_timeout_seconds": 900,
        "max_iterations": 8, "mcp_ui_supervise": False, "mcp_ui_port": 8770,
        "mcp_tool_reload_seconds": 3})
    assert r["ok"] is True
    cfg = Config.load(env)
    assert cfg.max_cost_usd == 5.5
    assert cfg.max_tokens == 250000
    assert cfg.turn_timeout_seconds == 900
    assert cfg.max_iterations == 8
    assert cfg.mcp_ui_supervise is False
    assert cfg.mcp_ui_port == 8770
    assert cfg.mcp_tool_reload_seconds == 3
    got = studio.settings_payload(env)
    assert got["max_cost_usd"] == 5.5 and got["mcp_ui_port"] == 8770

def test_blank_disables_and_negatives_rejected(tmp_path):
    env = _env(tmp_path)
    r = studio.save_loop_mcp_settings(env, {"max_cost_usd": "", "max_tokens": 0})
    assert r["ok"] is True
    cfg = Config.load(env)
    assert cfg.max_cost_usd == 0.0 and cfg.max_tokens == 0
    bad = studio.save_loop_mcp_settings(env, {"max_tokens": -10})
    assert bad["ok"] is False and "negative" in bad["error"].lower()

def test_idempotent_second_write_updates_in_place(tmp_path):
    env = _env(tmp_path)
    studio.save_loop_mcp_settings(env, {"max_cost_usd": 1.0})
    studio.save_loop_mcp_settings(env, {"max_cost_usd": 2.0})
    text = (env / "harn.toml").read_text()
    assert text.count("max_cost_usd") == 1
    assert Config.load(env).max_cost_usd == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_settings_save.py -v`
Expected: FAIL — `AttributeError: module 'harn.studio' has no attribute 'save_loop_mcp_settings'`

- [ ] **Step 3: Implement**

Add to `harn/studio.py`:

```python
def settings_payload(env_dir: Path) -> dict:
    cfg = Config.load(env_dir)
    return {"max_cost_usd": cfg.max_cost_usd, "max_tokens": cfg.max_tokens,
            "turn_timeout_seconds": cfg.turn_timeout_seconds,
            "max_iterations": cfg.max_iterations,
            "mcp_ui_supervise": cfg.mcp_ui_supervise,
            "mcp_ui_port": cfg.mcp_ui_port,
            "mcp_tool_reload_seconds": cfg.mcp_tool_reload_seconds}


# key -> (section, kind); kind: "float" | "int" | "bool"
_SETTINGS_KEYS = {
    "max_cost_usd": ("loop", "float"), "max_tokens": ("loop", "int"),
    "turn_timeout_seconds": ("loop", "int"), "max_iterations": ("loop", "int"),
    "mcp_ui_supervise": ("mcp", "bool"), "mcp_ui_port": ("mcp", "int"),
    "mcp_tool_reload_seconds": ("mcp", "int"),
}
# UI/config key name -> the harn.toml key name (they differ for the mcp_* ones).
_SETTINGS_TOML_KEY = {
    "mcp_ui_supervise": "ui_supervise", "mcp_ui_port": "ui_port",
    "mcp_tool_reload_seconds": "tool_reload_seconds",
}


def _coerce_setting(kind: str, raw):
    """Blank -> 0/False; validate type; reject negatives for numerics.
    Returns (value, error)."""
    if kind == "bool":
        return bool(raw), ""
    if raw in ("", None):
        return (0.0 if kind == "float" else 0), ""
    try:
        val = float(raw) if kind == "float" else int(raw)
    except (TypeError, ValueError):
        return None, f"{raw!r} is not a number"
    if val < 0:
        return None, "value cannot be negative"
    return val, ""


def save_loop_mcp_settings(env_dir: Path, payload: dict) -> dict:
    toml_path = env_dir / "harn.toml"
    text = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    saved = {}
    for key, raw in payload.items():
        spec = _SETTINGS_KEYS.get(key)
        if not spec:
            continue
        section, kind = spec
        val, err = _coerce_setting(kind, raw)
        if err:
            return {"ok": False, "error": err}
        toml_key = _SETTINGS_TOML_KEY.get(key, key)
        literal = ("true" if val else "false") if kind == "bool" else repr(val)
        text = _set_toml_kv(text, section, toml_key, literal)
        saved[key] = val
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(text, encoding="utf-8")
    return {"ok": True, "error": "", **saved}


def _set_toml_kv(text: str, section: str, key: str, literal: str) -> str:
    """Targeted in-place edit of `[section] key = literal` (stdlib can't write
    TOML). Same regex idiom as save_defaults/set_config_flag."""
    line = f"{key} = {literal}"
    if re.search(rf"(?m)^\s*{key}\s*=.*$", text) and \
       re.search(rf"(?ms)^\[{section}\].*?^\s*{key}\s*=", text):
        return re.sub(rf"(?m)^\s*{key}\s*=.*$", line, text, count=1)
    if re.search(rf"(?m)^\[{section}\]\s*$", text):
        return re.sub(rf"(?m)^\[{section}\]\s*$", f"[{section}]\n{line}", text, count=1)
    return (text.rstrip() + f"\n\n[{section}]\n{line}\n") if text else \
        f"[{section}]\n{line}\n"
```

Note: `_set_toml_kv`'s "key exists" branch guards that the key is under the right section to avoid cross-section collisions on shared key names (there are none today, but this keeps it safe). Routes — GET (`do_GET`):

```python
            elif route == "/api/settings":
                self._json(settings_payload(env))
```

POST (`do_POST`):

```python
            elif route == "/api/settings":
                self._json(save_loop_mcp_settings(env, body))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_settings_save.py tests/ -q`
Expected: PASS — 3 new tests green, full suite green.

- [ ] **Step 5: Bump version + commit**

`0.17.54`.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml tests/test_settings_save.py
git commit -m "feat(studio): settings backend for loop budget + mcp knobs (Phase 8); version 0.17.54"
```

---

## Task 6: studio UI — Settings sections, MCP header badge, run-banner spend

**Files:**
- Modify: `harn/studio.py` — JS: extend `renderSettings()` with "Loop & safety" + "MCP" sections and `saveLoopMcpSettings()`; add `pollMcpHealth()` + header badge element; add spend/budget to the run banner (`renderRunning`, ~line 1990)
- Modify: `harn/__init__.py`, `pyproject.toml` (version bump)
- Verify: `node --check` + live preview click-through (no JS unit harness in this repo)

**Interfaces:**
- Consumes: `/api/settings` (GET/POST), `/api/mcp/health` (GET), `/api/mcp/restart` (POST) from Tasks 4–5.

- [ ] **Step 1: Add the header MCP badge element + poller**

In the header HTML (near the status area, `~line 59`'s `setStatus` span region), add a badge span:

```html
      <span id="mcpBadge" class="mcpbadge" title="harn MCP status" style="display:none"></span>
```

Add CSS (near `.settings` styles):

```css
  .mcpbadge{font-size:11px;padding:3px 8px;border-radius:999px;border:1px solid var(--line);
    cursor:pointer;white-space:nowrap}
  .mcpbadge.live{color:var(--accent2);border-color:#3ad6a055}
  .mcpbadge.stale{color:var(--warn);border-color:#e8b93a66}
  .mcpbadge.down{color:var(--danger);border-color:#ff6b6b66}
```

Add JS (near `load()`'s `setInterval`):

```javascript
let MCP_HEALTH=null;
async function pollMcpHealth(){
  try{ MCP_HEALTH=await (await fetch(api('/api/mcp/health'))).json(); }
  catch(e){ return; }
  const b=$('#mcpBadge'); if(!b) return;
  const h=MCP_HEALTH; b.style.display='';
  if(!h.running){ b.className='mcpbadge down'; b.textContent='MCP ○ down — Restart'; }
  else if(h.stale){ b.className='mcpbadge stale'; b.textContent='MCP ▲ stale — Reload'; }
  else { b.className='mcpbadge live'; b.textContent=`MCP ● live · ${h.tools_count} tools`; }
  b.onclick=restartMcp;
}
async function restartMcp(){
  const b=$('#mcpBadge'); if(b) b.textContent='MCP … restarting';
  const r=await post_('/api/mcp/restart',{});
  if(!r.ok) alert(r.error||'restart failed');
  setTimeout(pollMcpHealth, 1500);
}
```

Call `pollMcpHealth()` once in `load()` and add it to the existing 1.5s interval:

```javascript
  setInterval(()=>{ pollProgress(); pollBoard(); pollMcpHealth(); }, 1500);
  pollMcpHealth();
```

- [ ] **Step 2: Extend `renderSettings()` with the two sections**

After the existing default-agent/model fields' closing (before the trailing note `<p class="mut">…chat mode…`), insert:

```javascript
    <h2 style="margin-top:26px">SETTINGS — loop & safety</h2>
    <div id="loopSafetyFields"></div>
    <h2 style="margin-top:26px">SETTINGS — MCP</h2>
    <div id="mcpFields"></div>
```

Add a loader used by `renderSettings()` (fetch current values then fill both blocks). Insert at the top of `renderSettings` after `ensureModelsLoaded()`:

```javascript
  let SETTINGS={}; try{ SETTINGS=await (await fetch(api('/api/settings'))).json(); }catch(e){}
```

And after `v.innerHTML=...` assignment, populate:

```javascript
  const numField=(id,label,val,hint)=>`<div class="field"><label>${label} <span class="mut">${hint}</span></label>`+
    `<input type="number" id="${id}" value="${val}" min="0" step="any"/></div>`;
  $('#loopSafetyFields').innerHTML=
    numField('setMaxCost','Max cost per run (USD)',SETTINGS.max_cost_usd,'(0 = unlimited)')+
    numField('setMaxTokens','Max tokens per run',SETTINGS.max_tokens,'(0 = unlimited)')+
    numField('setTurnTimeout','Per-turn timeout (seconds)',SETTINGS.turn_timeout_seconds,'(0 = adapter default 1800)')+
    numField('setMaxIters','Max iterations per run',SETTINGS.max_iterations,'(turn-count ceiling)')+
    `<button class="primary" onclick="saveLoopMcp()">Save loop & MCP settings</button> <span id="lmStatus" class="status"></span>`;
  $('#mcpFields').innerHTML=
    `<div class="field"><label><input type="checkbox" id="setSupervise" ${SETTINGS.mcp_ui_supervise?'checked':''}/> harn ui supervises an MCP server</label></div>`+
    numField('setMcpPort','MCP port',SETTINGS.mcp_ui_port,'(harn mcp --http)')+
    numField('setReload','Tool hot-reload (seconds)',SETTINGS.mcp_tool_reload_seconds,'(0 = disable live reload)');
```

Add the save handler:

```javascript
async function saveLoopMcp(){
  const num=id=>{const v=$('#'+id)?$('#'+id).value.trim():''; return v===''?'':v;};
  $('#lmStatus').textContent='saving…';
  const r=await post_('/api/settings',{
    max_cost_usd:num('setMaxCost'), max_tokens:num('setMaxTokens'),
    turn_timeout_seconds:num('setTurnTimeout'), max_iterations:num('setMaxIters'),
    mcp_ui_supervise:$('#setSupervise').checked, mcp_ui_port:num('setMcpPort'),
    mcp_tool_reload_seconds:num('setReload')});
  $('#lmStatus').textContent = r.ok ? 'saved ✓ (restart harn ui for MCP changes)' : (r.error||'save failed');
}
```

- [ ] **Step 3: Add live spend/budget to the run banner**

In `renderRunning()` (~line 1991), where the running-workflow kv rows are built, add a spend row driven by `MCP_HEALTH`-independent data already present in the banner. Use `PROG`/board tokens if available, else read from `MCP_HEALTH` is wrong — instead surface the budget from `SETTINGS` (fetched) and live tokens/cost from the existing progress metrics. Add:

```javascript
      <div class="kv"><span>budget</span><b id="runBudget" class="mut">—</b></div>
```

and after render, set it from a fetched settings + progress metrics (reuse the `tokens`/`cost` the banner already computes at ~line 1996–2000):

```javascript
  try{
    const s=await (await fetch(api('/api/settings'))).json();
    const capC=s.max_cost_usd||0, capT=s.max_tokens||0;
    const el=$('#runBudget'); if(el){
      el.textContent=(capC||capT)
        ? `${capC?('$'+ (window.__runCost||0).toFixed(2)+'/$'+capC.toFixed(2)):''} ${capT?((window.__runTok||0)+'/'+capT+' tok'):''}`.trim()
        : 'unlimited';
    }
  }catch(e){}
```

(Where the banner already computes cost/tokens, assign them to `window.__runCost`/`window.__runTok` so the budget row can read them. If those aren't computed there yet, read the last `stage_end` metrics from `PROG`.)

- [ ] **Step 4: Verify JS + live click-through**

```bash
python3 - <<'PY'
import re
src=open('harn/studio.py').read()
m=re.search(r'<script>(.*?)</script>', src, re.DOTALL)
open('/tmp/studio_p8.js','w').write(m.group(1))
PY
node --check /tmp/studio_p8.js && echo OK
```

Then start a FRESH `harn-studio` preview, open the demo project via `?env=`, and confirm: (a) header shows an MCP badge; (b) Settings tab renders the two new sections with current values; (c) saving updates `harn.toml` (check the file); (d) `node --check` clean; (e) no console errors. Expected: all pass.

- [ ] **Step 5: Bump version + commit**

`0.17.55`.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml
git commit -m "feat(studio): Settings loop/MCP sections + MCP header badge + run budget row (Phase 8); version 0.17.55"
```

---

## Task 7: Docs + template + final version

**Files:**
- Modify: `harn/templates/harn.toml` (document the six new keys with their defaults, commented)
- Modify: `README.md` or `docs/` (whichever documents config — grep for `max_iterations` to find the canonical config doc)
- Modify: `harn/__init__.py`, `pyproject.toml` (version bump)

- [ ] **Step 1: Add the keys to the template**

In `harn/templates/harn.toml`, under `[loop]` and `[mcp]`, add commented lines with defaults:

```toml
[loop]
# ... existing keys ...
# Run-level spend ceilings (0 = unlimited). Crossing either stops the run and
# BLOCKS it — a single runaway turn can't quietly burn the budget.
max_cost_usd = 3.0
max_tokens = 400000
# Per-turn subprocess timeout in seconds (0 = the adapter's own 1800s default).
turn_timeout_seconds = 1800

[mcp]
# ... existing keys ...
# harn ui supervises an owned `harn mcp --http` child on this port, restarts it
# if it dies, and shows its health in the header. Set false to opt out.
ui_supervise = true
ui_port = 8765
# The MCP server hot-reloads custom tools from harn_env/tools/ this often, so a
# tool you add mid-session becomes callable without restarting (0 = disable).
tool_reload_seconds = 2
```

- [ ] **Step 2: Document in the config reference**

Find the canonical config doc (`grep -rn "max_iterations" README.md docs/ 2>/dev/null`) and add a short row/paragraph for each of the six keys, matching that doc's existing format. If no such doc exists, add a "Loop budget & MCP supervision" subsection to `README.md` near the existing config discussion.

- [ ] **Step 3: Verify docs build / render**

If docs are markdown only, visually confirm the new keys read correctly. Run the full suite once more:

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all).

- [ ] **Step 4: Bump version + commit**

`0.17.56`.

```bash
git add harn/templates/harn.toml README.md docs/ harn/__init__.py pyproject.toml
git commit -m "docs: document loop budget + MCP supervision/reload knobs (Phase 8); version 0.17.56"
```

---

## Notes for the executor

- **Reliability is the point** (user: "делай надёжно, чтобы нельзя было сломать случайно"): the budget guard must fire even when config is malformed (defaults win in `Config.load`), the watcher/supervisor threads must never raise into a request or a turn (wrap bodies in `try/except`, daemon threads), `0` must cleanly disable each guard, and settings save must reject bad input rather than write malformed TOML. Reviewers: treat a path where a thread exception could crash a request, or where a negative/blank value could slip through to a live budget, as a Critical finding.
- **Don't touch** Phase 5's custom-tool execution/security path (`tools.py` `save`/`import_bundle`/`execute`, `_make_tool_function`'s param handling) except to CALL it — the `is_safe_param_name` re-validation in Task 3 is the only security-adjacent addition and must be preserved exactly.
- If `send_tool_list_changed` / `anyio.from_thread` isn't reachable in this `mcp` version, the notification silently no-ops — that's acceptable (the ToolManager is still correct). Do NOT block reconcile on the notification.
- Confirm `AgentResult.total_tokens` exists (`grep -n total_tokens harn/adapters/base.py`); if it's named differently, use `(res.input_tokens or 0) + (res.output_tokens or 0)` in Task 2.
