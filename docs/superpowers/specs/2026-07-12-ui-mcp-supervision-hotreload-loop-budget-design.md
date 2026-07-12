# UI-supervised MCP + hot-reload custom tools + loop budget (Phase 8)

## Problem

Two independent failures, seen live on a real run (task PRJ-044):

1. **Stale MCP / un-callable custom tools.** Custom tools register only when
   an MCP server *boots* (`mcp_server.build_server()`, the `register_custom`
   loop). Agents reach harn through a stdio subprocess spawned from
   `.mcp.json`/`.cursor/mcp.json` (`scaffold._mcp_command`). An IDE keeps that
   subprocess alive for the whole session, so a server that started *before*
   you created a custom tool never sees it. The agent sees the tool name in
   its step but cannot call it — and there is no signal to the user that the
   running MCP is stale while the studio UI keeps working normally.

2. **No spend ceiling on a run.** The loop caps *turn count*
   (`[loop] max_iterations = 10`) but nothing caps *tokens or cost*. A single
   agent turn spinning on the un-callable tool burned ~500k tokens; the loop
   then retried, burning ~500k more. `max_iterations` cannot stop a single
   runaway turn. The run sat at "starting…" in the UI while ~1.5M tokens /
   ~$0.37 evaporated across three attempts.

The user also requires: **every parameter this phase introduces must appear in
the studio Settings tab, editable and disableable from the UI** — no
config-file-only knobs.

## Design

Three parts, one branch. Parts A and C are independent; Part B builds on A.

### Part A — Hot-reload custom tools (root-cause fix for staleness)

A background watcher, started inside `build_server()` when `register_custom`
is on (mirroring the existing `_ensure_watch_running` auto-start pattern),
polls `harn_env/tools/` every `[mcp] tool_reload_seconds` (default 2s) using a
cheap signature of the directory (each `*.json`'s name + mtime + size). On a
change it re-runs `tools_mod.discover()`, diffs against the currently
registered custom tools, and reconciles the live `ToolManager`:

- **added / changed** → `mcp._tool_manager.remove_tool(name)` if present, then
  `mcp.add_tool(fn, name=…, description=…)` with a freshly synthesized
  function (same `_make_tool_function` path, same `is_safe_param_name`
  defense-in-depth re-validation already in the boot loop — a tool with an
  unsafe param name is skipped with a logged warning, never registered).
- **removed** (json deleted) → `mcp._tool_manager.remove_tool(name)`.
- Built-in tool names are never touched (the watcher only ever reconciles the
  set discovered under `harn_env/tools/`).

After any reconciliation the watcher makes a **best-effort** attempt to notify
connected clients that the tool list changed (the MCP `tools/list_changed`
capability, which FastMCP advertises when tools exist). If no active session
is reachable from the watcher thread, the reconciliation still stands: the
`ToolManager._tools` dict is now correct, so the next `tools/list` and the
next `call_tool` succeed regardless. This is what makes *every* `harn mcp`
self-heal:

- **Headless `harn run`** (each `claude -p` spawns a fresh MCP subprocess):
  always current, plus reload covers a tool created mid-run.
- **Long-lived IDE stdio server**: the tool dict goes current within
  `tool_reload_seconds`; the client re-lists on the `list_changed`
  notification (or its next natural re-list) and can then call the tool — no
  IDE restart, no manual intervention.

The watcher is daemon, best-effort, and never raises into request handling —
a stat error or a malformed json is logged and skipped, exactly like the boot
loop already tolerates.

### Part B — `harn ui` supervises an MCP + health badge

`harn ui` (studio) gains an owned, supervised MCP child so "MCP dead while UI
alive" becomes visible and self-correcting for the server harn controls.

- **Supervision.** On `studio.serve()` startup, spawn `harn mcp --http` (host
  `127.0.0.1`, port `[mcp] ui_port`, default 8765) as a child, recording
  `state/ui_mcp.pid`. A small monitor thread restarts it if it dies
  (bounded: at most 5 restarts within a rolling 60s window, then surfaces
  "down" instead of hot-looping). On studio shutdown (Ctrl-C / `server_close`), terminate the
  child (SIGTERM, pid file removed). Supervision is **opt-outable**: `[mcp]
  ui_supervise = false` (default true) makes `harn ui` skip starting/owning a
  server — for users whose agents already point at their own server and who
  don't want a second one.
- **Health surface.** A new route `GET /api/mcp/health` returns, for the
  active project: `{ running: bool, port, tools_count, custom_names: [...],
  disk_custom_names: [...], stale: bool, error: str|"" }`. `running` and
  `tools_count` come from the existing `mcp_server.healthcheck(env)`;
  `stale` is `disk_custom_names != custom_names` (a tool exists on disk that
  the live server isn't serving — the exact condition that bit PRJ-044).
- **Header badge.** A status chip in the studio header, polled on the existing
  1.5s tick: **`MCP ● live · N tools`** (green), **`MCP ▲ stale — Reload`**
  (amber, when `stale`), or **`MCP ○ down — Restart`** (red). Clicking
  Reload/Restart calls `POST /api/mcp/restart`, which stops and re-spawns the
  supervised child (a fresh boot re-registers all tools; with Part A, live
  reload usually clears `stale` on its own within ~2s, so the button is the
  manual backstop).

**Locked scope decision (surfaced for review):** agents are **not**
auto-migrated from stdio to this HTTP server. Codex / Antigravity / Qwen
stdio-vs-HTTP compatibility is not something to flip blind, and Part A already
makes the agents' own stdio servers self-heal. The supervised HTTP server is
for (a) the health/liveness visibility the user asked for, (b) the studio's
own tool operations, and (c) a guaranteed-fresh server for anything harn
launches. Making it the single shared server that all agents use (an HTTP
`.mcp.json` scaffold) is a **documented follow-up, not in this phase**.

### Part C — Loop budget: cost + tokens (guaranteed stop)

New `[loop]` knobs:

- `max_cost_usd` — default **3.0**; `0` = unlimited.
- `max_tokens` — default **400000**; `0` = unlimited.
- (Also surfaced, already exists) `turn_timeout_seconds` — the per-turn
  subprocess timeout, currently the hard-coded `1800` default of
  `Adapter.run_turn`. Promote it to `[loop] turn_timeout_seconds` (default
  1800) so a single turn's blast radius is tunable from the UI. `0` keeps the
  adapter default.

Enforcement in `loop.run()`: `tok_totals` / `tok_costs` already accumulate per
turn (`_accumulate`, called from `_run_turn`). After each agent turn, compute
the **run-cumulative** spend by summing across all task keys. If
`max_cost_usd > 0 and cost >= max_cost_usd`, or `max_tokens > 0 and tokens >=
max_tokens`:

- Write `state/BLOCKED.md` with a precise reason:
  `"Run stopped: budget exceeded — spent $X.XX / N tokens, cap $3.00 / 400k.
  Raise the budget in Settings or split the task, then Resume."`
- `st.block(reason)`, save state (phase → BLOCKED), and `return
  _run_end(...)` — stop **before** the next turn.
- Emit an `error`/`block` event so `harn trace` and the board show why.

A single turn can still spend up to `turn_timeout_seconds` worth once; the
budget catches it on the very next check (the second turn never starts). This
is deterministic — no heuristic "is it looping" guesswork.

The studio run banner (`renderRunning`) shows live **spend / budget**
(`$0.14 / $3.00 · 545k / 400k tok`) so the climb is visible, turning amber as
it approaches the cap.

### Settings tab — every new parameter, editable + disableable

The Settings tab currently edits only `[harn] agent`/`model` via
`/api/defaults` → `save_defaults` (targeted `[harn]` TOML edits). This phase
adds a **"Loop & safety"** and an **"MCP"** section to the same tab, wired to
a generalized save so each knob is set or cleared from the UI:

- **Loop & safety:** `max_cost_usd`, `max_tokens`, `turn_timeout_seconds`,
  `max_iterations` (existing, now surfaced). Each is a number input with an
  explicit **"unlimited / off"** affordance (empty or 0) so protection can be
  disabled deliberately from the UI.
- **MCP:** `ui_supervise` (on/off toggle), `ui_port`, `tool_reload_seconds`
  (0 = disable hot-reload), plus the live health badge mirrored here with the
  Reload/Restart control.

Backend: a new `save_loop_mcp_settings(env_dir, payload)` doing the same
targeted-regex `[loop]` / `[mcp]` table edits `save_defaults`/`set_config_flag`
already use (stdlib can't write TOML), validating each numeric field (reject
negative, coerce blank→0) and echoing back the saved values. A new `settings_payload(env_dir)` returns
the current `[loop]`/`[mcp]` values (with defaults filled in) so the tab
renders what's actually in `harn.toml`.

## Non-goals

- Migrating agents onto the UI-supervised HTTP server / an HTTP `.mcp.json`
  scaffold (documented follow-up).
- Per-turn *token* cap (harn can't see inside a running subprocess; the lever
  for a single turn is `turn_timeout_seconds`). The budget is enforced
  between turns.
- Per-task (as opposed to per-run) budgets. A run is the unit; for the
  studio's single-task Launch, per-run == per-task anyway.
- Changing the security model of custom-tool execution (Phase 5's reviewed
  `shlex`/param-validation path is reused verbatim).

## Testing

- **Hot-reload (A):** create a tool file after `build_server()`, tick the
  watcher once, assert the `ToolManager` now lists it and can build its
  function; delete the file, tick, assert it's gone; a tool with an unsafe
  param name is skipped (not registered), never raising. `list_changed`
  best-effort notify is exercised behind a mock session (no real transport).
- **Supervision (B):** `save`/health payload unit tests — `stale` true when a
  disk tool is absent from the live list, false when they match; `running`
  false → badge "down". Studio startup/shutdown starting and reaping the child
  is covered by a launch/stop test with a fake subprocess (same style as
  `runner`'s tests), not a real MCP boot.
- **Loop budget (C):** a `run()` unit test with a stub adapter whose turns
  report known `cost_usd`/tokens: assert the loop stops and writes BLOCKED the
  turn the cumulative crosses `max_cost_usd`, likewise for `max_tokens`; `0`
  disables each ceiling (runs to normal completion); the BLOCKED reason string
  contains the actual spend and the cap.
- **Settings (UI):** `save_loop_mcp_settings` writes/updates each `[loop]` and
  `[mcp]` key idempotently, rejects negatives, treats blank as 0/unlimited;
  round-trips through `settings_payload`. JS verified via `node --check` +
  live click-through (this repo's established studio-UI verification method).

## Global constraints

- Bump `__version__` (harn/__init__.py) and `pyproject.toml` on **every**
  committed change under `harn/` (standing rule), not only at phase end.
- Pure stdlib in the loop/mcp/config paths (harn stays lean; the only
  dependency is the already-present `mcp` SDK).
- Reuse existing patterns: targeted-regex TOML edits for config writes; the
  `_ensure_watch_running` daemon idiom for the tool watcher; the `runner`
  subprocess-with-pidfile idiom for MCP supervision; `is_safe_param_name`
  re-validation before any `exec()`-synthesized tool function.
