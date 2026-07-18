# Flow Step Live Transcript Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream and persist a readable Codex-style transcript for every Flow step and render it live inside that step's progress card.

**Architecture:** Add a provider-neutral adapter event callback and a dedicated append-only transcript store. Codex emits normalized events while `codex exec --json` is still running; the loop tags them with task/step/run/attempt, Studio exposes cursor-based reads, and the sidebar appends only new entries without rebuilding open cards.

**Tech Stack:** Python stdlib (`subprocess`, JSONL, `http.server`), existing harn adapter/loop/event architecture, vanilla JavaScript/CSS, pytest, Playwright MCP.

## Global Constraints

- Do not render raw provider JSONL or hidden chain-of-thought.
- Preserve transcripts across completion, failure, reload, and Studio restart.
- Associate every entry with one task, step, run, and attempt.
- Retain the existing 1.5-second polling cadence; do not add WebSockets/SSE.
- Keep `step_results` as the compact ledger and transcript JSONL as detailed history.
- Preserve sidebar disclosure state and scroll while live entries arrive.
- Adapters without structured streaming emit coarse lifecycle and final response entries.
- Bump the harn package version when the feature is complete.

---

### Task 1: Persistent normalized transcript store

**Files:**
- Create: `harn/transcript.py`
- Create: `tests/test_transcript.py`

**Interfaces:**
- Produces: `append(env_dir, *, task_id, step_id, run_id, attempt, kind, phase, title="", text="", item_id="") -> dict`
- Produces: `read(env_dir, *, task_id, step_id=None, after=0, limit=500) -> dict` returning `{entries, cursor}`.

- [x] **Step 1: Write failing store tests**

Cover monotonic sequence numbers, task/step filtering, `after` cursor, attempt retention, malformed-line tolerance, and limit enforcement using a temporary env.

- [x] **Step 2: Verify red**

Run: `pytest tests/test_transcript.py -q`

Expected: import failure because `harn.transcript` does not exist.

- [x] **Step 3: Implement the store**

Use `state/step_transcript.jsonl`, UTC timestamps, append-only JSON lines, and a sequence derived under a module lock. `append` returns the exact stored record. `read` exposes only normalized keys and skips malformed rows.

- [x] **Step 4: Verify green**

Run: `pytest tests/test_transcript.py -q`

Expected: all transcript store tests pass.

---

### Task 2: Adapter streaming boundary and Codex JSONL normalization

**Files:**
- Modify: `harn/adapters/base.py`
- Modify: `harn/adapters/codex.py`
- Modify: `tests/test_adapters.py`
- Create or modify: `tests/test_codex_streaming.py`

**Interfaces:**
- Consumes: callback `on_event(event: dict) -> None`.
- Produces: `Adapter.run_turn(..., on_event=None) -> AgentResult` on all adapters.
- Produces: `CodexAdapter._normalize_event(data: dict) -> list[dict]`.

- [x] **Step 1: Write failing normalization tests**

Feed representative `item.started`/`item.completed` records for
`agent_message`, `reasoning`, `command_execution`, `file_change`,
`mcp_tool_call`, `collab_tool_call`, and `web_search`; assert normalized
`kind`, `phase`, `title`, `text`, and `item_id`. Assert `turn.completed` usage
maps input, cached input, output, and reasoning-output tokens.

- [x] **Step 2: Write a failing live-read test**

Replace process construction with a delayed fake process whose stdout yields
one JSON line before completion. Assert `on_event` receives that entry before
the fake process reports its final return code.

- [x] **Step 3: Verify red**

Run: `pytest tests/test_adapters.py tests/test_codex_streaming.py -q`

Expected: callback/signature and normalizer assertions fail.

- [x] **Step 4: Extend the base interface compatibly**

Add keyword-only `on_event=None` to adapter methods. The base captured-output
path emits `status/started`, then a visible final `message/completed` or
`error/failed`, so non-streaming adapters still populate a transcript.

- [x] **Step 5: Implement Codex streaming**

Invoke `codex exec --json <prompt>` through `subprocess.Popen` with line-
buffered text stdout and separate stderr. Parse each complete stdout line,
normalize known public item types, call `on_event` immediately, accumulate the
final visible agent message and usage into `AgentResult`, and skip unknown or
malformed JSON without aborting the turn. Do not expose reasoning content
beyond Codex's public `summary` field.

- [x] **Step 6: Verify green and adapter compatibility**

Run: `pytest tests/test_adapters.py tests/test_codex_streaming.py -q`

Expected: all tests pass, including existing adapters that ignore callbacks.

---

### Task 3: Loop tagging and transcript lifecycle

**Files:**
- Modify: `harn/loop.py`
- Modify: `tests/test_step_run.py`
- Modify: `tests/test_step_attempt_cap.py`

**Interfaces:**
- Consumes: `transcript.append(...)` and adapter `on_event`.
- Produces: `_transcript_callback(env_dir, task, step_id, attempt) -> Callable`.

- [x] **Step 1: Write failing loop tests**

Use a fake adapter that synchronously emits status, tool, and message entries.
Assert records carry the correct task id, step id, current run id, and attempt.
Run the step twice and assert attempts remain distinct. Assert a failing turn
retains its transcript.

- [x] **Step 2: Verify red**

Run the exact new loop tests with `pytest ... -q` and confirm missing transcript
records cause the failure.

- [x] **Step 3: Wire the callback into every agent step turn**

Create the callback immediately after `step_results[sid]` becomes running and
pass it through `_run_turn` into `adapter.run_turn`. Emit a final response only
when the adapter did not already emit an equivalent agent-message item. Keep
command steps readable by emitting command start/output/completion entries.

- [x] **Step 4: Verify green and existing loop behavior**

Run: `pytest tests/test_step_run.py tests/test_step_attempt_cap.py tests/test_step_enforcement.py tests/test_command_steps.py -q`

Expected: all selected loop tests pass.

---

### Task 4: Cursor-based Studio transcript API

**Files:**
- Modify: `harn/studio.py`
- Modify: `tests/test_studio.py`

**Interfaces:**
- Consumes: `transcript.read(env_dir, task_id=..., step_id=..., after=..., limit=...)`.
- Produces: `GET /api/tasks/transcript?task=<id>&step=<optional>&after=<seq>`.

- [x] **Step 1: Write failing payload and route tests**

Assert missing task is rejected, task filtering prevents cross-task leakage,
step filtering works, and `after` returns only unseen entries plus the newest
cursor.

- [x] **Step 2: Verify red**

Run the new Studio API tests and confirm the endpoint/payload is missing.

- [x] **Step 3: Implement payload and GET route**

Parse `after` safely as a non-negative integer, cap `limit` at 500, and return
`{ok: true, entries: [...], cursor: N}`. The route is read-only and never
changes task state.

- [x] **Step 4: Verify green**

Run: `pytest tests/test_studio.py -q`

Expected: API and existing Studio tests pass.

---

### Task 5: Live per-step transcript UI

**Files:**
- Modify: `harn/studio.py` (`_HTML` CSS and JavaScript)
- Modify: `tests/test_studio.py`

**Interfaces:**
- Consumes: transcript API entries and cursor.
- Produces: browser state keyed by `taskId -> stepId -> attempt` and targeted DOM updates.

- [x] **Step 1: Write failing HTML/JS behavior tests**

Assert the sidebar has transcript containers keyed by step id, a transcript
cursor, polling only while the run sidebar is open, targeted append logic,
attempt grouping, `Waiting for agent output…`, and no raw JSON rendering.

- [x] **Step 2: Verify red**

Run the new `tests/test_studio.py` selections and confirm transcript UI symbols
are absent.

- [x] **Step 3: Add transcript presentation styles**

Add readable message blocks, subdued status/commentary rows, compact command/
tool/skill rows, expandable results, error state, and live pulse. Keep widths
and overflow safe inside the existing 400px inspector.

- [x] **Step 4: Add incremental transcript state and polling**

When `RUN_HISTORY_OPEN`, request entries after the current cursor during the
existing 1.5-second tick. Merge by sequence id. Create each step card once and
append only new entry nodes into its transcript body. Automatically open the
active/latest attempt, preserve user disclosure choices, and auto-follow only
when the user is already near the bottom.

- [x] **Step 5: Integrate final-result fallback**

If historical runs have `step_results.output` but no transcript records, show
that output as a legacy final message so completed steps never display only a
counter.

- [x] **Step 6: Verify green**

Run: `pytest tests/test_studio.py -q`

Expected: all Studio tests pass.

---

### Task 6: Browser live verification and release version

**Files:**
- Modify: `pyproject.toml`
- Modify: `harn/__init__.py`
- Test artifact: Playwright snapshot/screenshot outside tracked source files.

**Interfaces:**
- Consumes: complete backend and frontend feature.
- Produces: verified release version and evidence of live, separated step feeds.

- [x] **Step 1: Build a delayed two-step test fixture**

Use a temporary harn environment and deterministic fake streamed entries. The
first step must emit at least two entries before completion; the second must
emit a different message.

- [x] **Step 2: Verify live behavior with Playwright MCP**

Click `RUN WORKFLOW`, assert the correct task plan appears, observe the first
step's message before it completes, then assert completion preserves it and the
second step's entries appear only in the second card. Capture a snapshot and
screenshot; check browser console errors.

- [x] **Step 3: Bump version**

Increase the patch version consistently in `pyproject.toml` and
`harn/__init__.py`.

- [x] **Step 4: Run final verification**

Run the focused transcript, adapter, loop, Studio, task-plan, and model test
suites, then `python -m harn --version` and `git diff --check`.

Expected: zero test failures, matching bumped version, and no whitespace errors.
