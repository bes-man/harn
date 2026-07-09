# Command Steps + On-Fail Transitions (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A workflow step can be `Type: command` (a shell command, no LLM, no tokens) with an optional `On fail: <step title>` that dispatches an agent-step handler on failure and retries the command afterward.

**Architecture:** Two new optional per-step fields (`type`, `command`, `on_fail`) parsed/composed in `harn/workflow.py`, `on_fail` stored by id and displayed by title (same rename-survival pattern as every other by-id reference in this codebase). `harn/loop.py`'s step engine (`run()` and `run_step()`) branches on `step["type"] == "command"`: runs `run_feedback()` instead of an agent turn, and on failure with a resolvable `on_fail` target, dispatches that handler step inline before retrying. `harn/studio.py` gets a Type toggle that swaps the existing Agent/Model block for a Command textarea + On-fail dropdown.

**Tech Stack:** Python stdlib only (existing constraint), pytest, single-file vanilla-JS studio (`harn/studio.py`).

**Spec:** `docs/superpowers/specs/2026-07-08-workflow-command-steps-onfail-design.md`

## Global Constraints

- Provider-agnostic: command steps have no agent/model at all (no LLM involved) — this is orthogonal to the existing per-agent system, not an extension of it.
- Stdlib-only in `harn/` (no new dependencies) — reuse `harn/feedback.py`'s existing `run_feedback(command, cwd, timeout=600) -> FeedbackResult` (`ran: bool, ok: bool, output: str`, `.tail(n=40)`).
- Failure = `not result.ok` (already true for both non-zero exit AND `subprocess.TimeoutExpired` inside `run_feedback` — no new logic needed there).
- `On fail:` only applies to `Type: command` steps. Storage is by id (`on_fail: "<step-id>"` in the parsed node dict), display/persistence in WORKFLOW.md is by the target's current TITLE — exactly mirroring how `Id:`/`stage_checkpoints`/`step_results` already survive renames. A stale/unresolvable reference degrades to `on_fail: ""`, never crashes.
- No new iteration-limit mechanism — the fail→handler→retry cycle consumes the SAME `for _ in range(limit)` budget `run()` already enforces (`cfg.max_iterations`/`cfg.auto_max_iterations`).
- A misconfigured `on_fail` (points at a command step, or at nothing resolvable at run time) degrades to "no on_fail" behavior and logs an `events.emit(env_dir, "config_error", ...)` — never raises.
- Every commit ends with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Run the full suite with `python3 -m pytest -q` from the repo root (baseline: 416 passed, confirmed at plan-writing time). JS syntax check via extracting the `<script>` block from `harn/studio.py` and running `node --check` (established pattern — see Task 3 Step 5).

---

### Task 1: `workflow.py` — `Type:`/`Command:`/`On fail:` fields (parse/compose)

**Files:**
- Modify: `harn/workflow.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: nothing new (works alongside the existing `id/agent/model/effort/temperature` fields from Phase 1).
- Produces: `parse()` node dicts (for `kind == "step"`) gain three new keys: `type` (`""` meaning `"agent"`, or `"command"`), `command` (`str`, `""` default), `on_fail` (`str`, the target step's `id`, `""` default). `compose()` writes `Type:`/`Command:`/`On fail:` lines when non-empty, resolving `on_fail`'s stored id back to that step's CURRENT title (or omitting the line if the id no longer resolves to any step in the same `nodes` list).

- [ ] **Step 1: Write the failing tests.** Add to `tests/test_workflow.py`, after the existing `test_renaming_step_does_not_move_the_stage_mapping`-era section (find the per-step-fields test block added in Phase 1 — search for `test_ensure_ids_fills_only_missing` — and add these tests immediately after it):

```python
# --- command steps + on-fail (Phase 2) --------------------------------- #

def test_parse_defaults_type_command_onfail_to_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    for n in parsed["nodes"]:
        if n["kind"] == "step":
            assert n["type"] == "" and n["command"] == "" and n["on_fail"] == ""


def test_command_step_round_trips_through_compose(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"] or "step-aaaaaa"
    if not implement_step["id"]:
        implement_step["id"] = "step-aaaaaa"
    workflow.save_parsed(env, parsed)

    reparsed = workflow.parse(env)
    t2 = next(n for n in reparsed["nodes"] if n["title"] == "Tests")
    assert t2["type"] == "command"
    assert t2["command"] == "npm test"
    assert t2["on_fail"] == implement_step["id"]
    # other steps stay untouched
    other = next(n for n in reparsed["nodes"] if n["title"] == "Verify")
    assert other["type"] == "" and other["command"] == "" and other["on_fail"] == ""


def test_compose_writes_onfail_as_target_title_not_id(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    workflow.ensure_ids(parsed)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"]
    text = workflow.compose(env, parsed)
    assert "On fail: Implement" in text
    assert implement_step["id"] not in text  # raw id never leaks into the file


def test_onfail_survives_target_rename(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    workflow.ensure_ids(parsed)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"]
    implement_step["title"] = "Build the thing"
    workflow.save_parsed(env, parsed)

    reparsed = workflow.parse(env)
    t2 = next(n for n in reparsed["nodes"] if n["title"] == "Tests")
    renamed = next(n for n in reparsed["nodes"] if n["title"] == "Build the thing")
    assert t2["on_fail"] == renamed["id"]


def test_onfail_dangling_reference_drops_to_empty_on_parse(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text(p.read_text().replace(
        "## 4. Tests", "## 4. Tests\nType: command\nCommand: npm test\n"
        "On fail: Not A Real Step Title"), encoding="utf-8")
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    assert step["type"] == "command" and step["command"] == "npm test"
    assert step["on_fail"] == ""


def test_compose_omits_onfail_line_when_target_deleted(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    workflow.ensure_ids(parsed)
    tests_step = next(n for n in parsed["nodes"] if n["title"] == "Tests")
    implement_step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    tests_step["type"] = "command"
    tests_step["command"] = "npm test"
    tests_step["on_fail"] = implement_step["id"]
    parsed["nodes"] = [n for n in parsed["nodes"] if n["title"] != "Implement"]
    text = workflow.compose(env, parsed)
    tests_block = text[text.index("## 4. Tests"):]
    tests_block = tests_block[:tests_block.index("\n## ", 1)] if "\n## " in tests_block[1:] else tests_block
    assert "On fail:" not in tests_block


def test_invalid_type_value_is_dropped_on_parse(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text(p.read_text().replace(
        "## 3. Implement", "## 3. Implement\nType: not_a_real_type"), encoding="utf-8")
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    assert step["type"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_workflow.py -q`
Expected: the seven new tests FAIL (`KeyError: 'type'` etc.).

- [ ] **Step 3: Implement in `harn/workflow.py`.**

(a) Add regexes near the existing `_ID_RE`/`_AGENT_RE`/etc. block (after `_TEMP_RE`, before `_STAGE_RE`):

```python
_TYPE_RE = re.compile(r"^\s*Type:\s*(\S+)\s*$", re.IGNORECASE)
_COMMAND_RE = re.compile(r"^\s*Command:\s*(.+)$", re.IGNORECASE)
_ONFAIL_RE = re.compile(r"^\s*On fail:\s*(.+?)\s*$", re.IGNORECASE)
_VALID_TYPES = {"", "agent", "command"}
```

(b) In `parse()`'s node-init dict (the `cur = {...}` literal inside the `if h:` branch), add three keys:

```python
cur = {"title": (m.group(2).strip() if m else raw),
       "required": [], "tools": [], "enabled": True,
       "id": "", "agent": "", "model": "", "effort": "",
       "temperature": "", "type": "", "command": "", "on_fail": "",
       "_num": bool(m), "_decl": False}
```

(c) In `parse()`'s line-matching chain, insert three new blocks right after the existing `temp_m` block and before the `body.append(line)` fallthrough:

```python
type_m = _TYPE_RE.match(line)
if type_m:
    cur["_decl"] = True
    val = type_m.group(1).strip().lower()
    cur["type"] = val if val in _VALID_TYPES else ""
    continue
cmd_m = _COMMAND_RE.match(line)
if cmd_m:
    cur["_decl"] = True
    cur["command"] = cmd_m.group(1).strip()
    continue
onfail_m = _ONFAIL_RE.match(line)
if onfail_m:
    cur["_decl"] = True
    cur["_on_fail_title"] = onfail_m.group(1).strip()  # resolved to an id in _flush()
    continue
```

(d) `On fail:` needs a SECOND pass after all nodes are collected (title→id resolution requires knowing every node's final id, including ids assigned later in the same file). Change `parse()`'s return statement from `return {"preamble": ..., "nodes": nodes}` to run a resolution pass first:

```python
    _flush()
    by_title = {n["title"]: n.get("id", "") for n in nodes if n.get("kind") == "step"}
    for n in nodes:
        raw_title = n.pop("_on_fail_title", "")
        n["on_fail"] = by_title.get(raw_title, "") if raw_title else n.get("on_fail", "")
    return {"preamble": "\n".join(preamble).strip(), "nodes": nodes}
```

(Note: `_flush()` already sets `cur["on_fail"] = ""` as part of the initial dict if no `On fail:` line was present, and does NOT pop `_on_fail_title` in that case since the key was never set — use `n.pop("_on_fail_title", "")` with a default so nodes without the line don't KeyError.)

(e) In `compose()`, extend the existing five-field loop (`for key, label in (("id", "Id"), ...)`) to include `type`/`command`, and add `on_fail` resolution AFTER that loop (needs a title lookup keyed by id, built once before the main node loop):

```python
def compose(env_dir: Path, parsed: dict) -> str:
    out = [parsed.get("preamble", "").strip(), ""]
    step_no = 0
    id_to_title = {n.get("id"): n["title"].strip() for n in parsed.get("nodes", [])
                   if n.get("kind") == "step" and n.get("id")}
    for n in parsed.get("nodes", []):
        ...
        for key, label in (("id", "Id"), ("agent", "Agent"), ("model", "Model"),
                           ("effort", "Effort"), ("temperature", "Temperature"),
                           ("type", "Type"), ("command", "Command")):
            val = str(n.get(key) or "").strip()
            if kind == "step" and val:
                out.append(f"{label}: {val}")
        if kind == "step":
            on_fail_id = str(n.get("on_fail") or "").strip()
            target_title = id_to_title.get(on_fail_id, "")
            if on_fail_id and target_title:
                out.append(f"On fail: {target_title}")
        out.append("")
```

(Fold this into the EXISTING loop body — don't duplicate the `for n in parsed.get("nodes", [])` line; the diff above shows where the new lines slot into the current structure. Re-read the current `compose()` body from the file before editing, since the exact surrounding lines matter for a clean edit.)

(f) Update `parse()`'s docstring to document the three new fields and the two-pass title→id resolution for `on_fail`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_workflow.py -q`
Expected: all pass (23 total: 16 from before Phase 2 + 7 new).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q 2>&1 | tail -10`
Expected: fully green, no collateral failures (this task only ADDS fields with safe defaults; nothing existing reads `type`/`command`/`on_fail` yet).

- [ ] **Step 6: Commit**

```bash
git add harn/workflow.py tests/test_workflow.py
git commit -m "feat(workflow): Type:/Command:/On fail: step fields (Phase 2)"
```

---

### Task 2: `loop.py` — command-step execution in `run()`

**Files:**
- Modify: `harn/loop.py`
- Test: `tests/test_command_steps.py` (create)

**Interfaces:**
- Consumes: `step["type"]`/`step["command"]`/`step["on_fail"]` from Task 1; existing `run_feedback(command, cwd, timeout=600) -> FeedbackResult`; existing `_checkpoint_stage`, `tasks._now_iso`, `events.emit`.
- Produces: `run()`'s step-turn branch (currently unconditional agent-turn code starting at the `if pending:` block, `step = pending[0]` line) gains a `step.get("type") == "command"` fork BEFORE the existing agent-turn code. New helper: `_run_command_step(env_dir, project_root, task, step, steps) -> str` returning one of `"advance"` (success, move to next step), `"handled"` (failure dispatched to a handler and retried — caller should `continue` the outer loop), `"blocked"` (handler itself blocked on `ask_user`).

Command-step semantics (implement exactly, matching the spec):

1. In `run()`, right after `step = pending[0]` / `sid = step.get("id") or ""` / `title = step.get("title", "")` are computed (existing code — do not change those three lines), branch:
   ```python
   if step.get("type") == "command":
       outcome = _run_command_step(env_dir, project_root, task, step, steps)
       if outcome == "blocked":
           return _run_end(env_dir, st)
       continue   # "advance" or "handled" — re-evaluate `pending` next iteration
   ```
   Place this branch BEFORE the existing `if not auto: tasks.set_status(...)` block (command steps have no agent/model, no auto-mode distinction needed — they always execute and always mutate the ledger, matching the "auto never mutates .md files" exception only for the AGENT-turn accounting; a command step's result is objective, not agent judgment, so `_run_command_step` writes the ledger unconditionally — confirm this is acceptable by writing the ledger write exactly like the existing agent-turn's NON-auto path, since command output is deterministic record-keeping, not "harness bookkeeping" in the sense the auto-mode comment means. If in doubt here mid-implementation, STOP and ask — this is the one genuinely ambiguous interaction between Phase 1's auto-mode purity and Phase 2's new step type).
2. `_run_command_step` body:
   ```python
   def _run_command_step(env_dir: Path, project_root: Path, task: "tasks.Task",
                         step: dict, steps: list[dict]) -> str:
       sid = step.get("id") or ""
       title = step.get("title", "")
       command = step.get("command", "")
       _checkpoint_stage(project_root, task, sid)
       started = tasks._now_iso()
       task.step_results[sid] = {"status": "running", "started": started, "ended": None}
       tasks._save(task)
       result = run_feedback(command, project_root)
       ended = tasks._now_iso()
       if result.ok:
           task.step_results[sid] = {"status": "ok", "started": started,
                                     "ended": ended, "output": result.tail(40)}
           tasks._save(task)
           progress.log(env_dir, f"{task.id}: {title} (command) — ok")
           return "advance"
       task.step_results[sid] = {"status": "failed", "started": started,
                                 "ended": ended, "output": result.tail(40)}
       tasks._save(task)
       progress.log(env_dir, f"{task.id}: {title} (command) — failed")
       on_fail_id = str(step.get("on_fail") or "").strip()
       handler = next((s for s in steps if s.get("id") == on_fail_id), None) \
           if on_fail_id else None
       if handler is None or handler.get("type") == "command":
           if on_fail_id:
               events.emit(env_dir, "config_error", task_id=task.id, stage=sid,
                           detail=f"on_fail target {on_fail_id!r} is not a live agent step")
           return "advance"   # no usable handler — record failure, move on (Phase-1-equivalent)
       return _run_onfail_handler(env_dir, project_root, task, step, handler)
   ```
3. `_run_onfail_handler` dispatches the handler as a normal agent turn (reusing the SAME machinery `run()`'s main branch uses — `_adapter_for_step`, `_build_step_prompt`, `_run_turn`, `_handle_block`), with one addition: the handler's prompt gets an extra context block about the failure. Add an `onfail_context: str = ""` parameter to `_build_step_prompt` (default `""`, backward compatible with Task 3's existing callers and `run_step()`) that, when non-empty, is appended as its own `parts.append(...)` block right after the step's own title/body block (before `_continuity_block`):
   ```python
   def _run_onfail_handler(env_dir: Path, project_root: Path, task: "tasks.Task",
                           failing_step: dict, handler: dict) -> str:
       cfg = Config.load(env_dir)
       adapter = _pick_adapter(cfg)
       handler_adapter = _adapter_for_step(cfg, handler, adapter)
       hid = handler.get("id") or ""
       result_entry = task.step_results.get(failing_step.get("id") or "", {})
       onfail_context = (
           f"## Triggered by a failed step\n**{failing_step.get('title', '')}** "
           f"failed:\n```\n{result_entry.get('output', '')}\n```")
       _checkpoint_stage(project_root, task, hid)
       tok_totals: dict = {}
       tok_costs: dict = {}
       prompt = _build_step_prompt(env_dir, cfg, task, handler,
                                   onfail_context=onfail_context)
       result = _run_turn(handler_adapter, env_dir, prompt, project_root,
                          task_id=task.id, stage=hid, step_title=handler.get("title", ""),
                          overrides=_step_overrides(cfg, handler),
                          tok_totals=tok_totals, tok_costs=tok_costs, cfg=cfg)
       state_dir = env_dir / "state"
       st = state.State.load(state_dir)
       b = _handle_block(env_dir, cfg, st, state_dir, task, auto=False)
       if b == "blocked":
           return "blocked"
       # resumed / auto / no-block: handler is DONE either way for this pass —
       # its own ledger entry records the attempt, then the engine retries
       # the original failing command step (NOT the handler) next iteration.
       task.step_results[hid] = {"status": "ok" if result.ok else "failed",
                                 "tokens": result.total_tokens}
       tasks._save(task)
       return "handled"
   ```
   **Important scoping note for the implementer:** `_handle_block`'s "resumed"/"auto" outcomes normally mean "re-run the SAME step" in `run()`'s main loop — but here, inside `_run_onfail_handler`, treat any non-"blocked" outcome as "handler attempt finished" and return `"handled"` unconditionally (do NOT loop internally re-running the handler — if the human answered a question mid-handler, that answer is already recorded in AGENTS/state; the NEXT top-level `run()` iteration will re-evaluate `pending` fresh). This is a deliberate simplification versus the main loop's retry logic — the handler gets exactly one attempt per failure, not its own internal retry loop. If this feels wrong once you're implementing it, STOP and ask rather than silently building a nested retry loop.
4. Back in `run()`: after `_run_command_step` returns `"handled"`, the CALLER's `continue` re-evaluates `pending` — since the handler step is now ledgered (`ok` or `failed`, either way NOT re-selected by `_step_done` unless it's literally `"ok"`... but the ORIGINAL failing command step's ledger entry is `"failed"`, not `"ok"`, so `_step_done(sid)` is still `False` for it and `pending[0]` naturally becomes the command step again on the next iteration — confirm this works via the test in Step 1 rather than assuming). This means NO special "jump back" bookkeeping is needed beyond what the ledger already provides — verify this claim empirically with a test before trusting it.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_command_steps.py`:

```python
"""Command steps (Type: command) + On-fail handler dispatch — Phase 2 of the
step engine. A command step runs a shell command instead of an agent turn
(harn/feedback.py's run_feedback); on failure with a resolvable On-fail
target, the engine dispatches that agent step, then retries the command."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import loop, tasks, workflows, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


class RecordingAdapter:
    name = "fake"
    def __init__(self, texts=None):
        self.calls = []
        self._texts = list(texts or ["fixed it"])
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        i = min(len(self.calls) - 1, len(self._texts) - 1)
        return AgentResult(ok=True, text=self._texts[i])


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _project(tmp_path, nodes):
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 20\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    workflows.save_task_plan(env, t.id, {"preamble": "", "nodes": nodes})
    return env, t


def _step(title, **kw):
    base = {"kind": "step", "title": title, "body": "", "id": "", "agent": "",
            "model": "", "effort": "", "temperature": "", "type": "",
            "command": "", "on_fail": "", "required": [], "tools": [],
            "enabled": True}
    base.update(kw)
    return base


def test_successful_command_step_advances_without_agent_call(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="true"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 0   # no agent turn at all
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "ok"


def test_failing_command_step_with_no_onfail_advances_and_records_failure(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="false"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "failed"
    assert fresh.status == tasks.REVIEW   # still submitted — Phase-1-equivalent, no special handling


def test_onfail_handler_dispatched_then_command_retried_until_it_passes(tmp_path, monkeypatch):
    """The classic case: tests fail -> Fix step runs -> tests re-run -> pass."""
    marker = tmp_path / "should_pass"
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command",
              command=f"test -f {marker}", on_fail="step-fix"),
        _step("Fix tests", id="step-fix"),
    ])
    class WritingAdapter(RecordingAdapter):
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                    temperature=None):
            self.calls.append({"prompt": prompt})
            marker.write_text("now it passes")
            return AgentResult(ok=True, text="fixed it")
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 1
    assert "Tests" in fake.calls[0]["prompt"] or "failed" in fake.calls[0]["prompt"].lower()
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "ok"
    assert fresh.step_results["step-fix"]["status"] == "ok"


def test_onfail_pointing_at_command_step_is_a_config_error_not_a_crash(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="false", on_fail="step-t2"),
        _step("Lint", id="step-t2", type="command", command="true"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)   # must not raise
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "failed"
    assert len(fake.calls) == 0


def test_persistent_failure_terminates_at_max_iterations_not_forever(tmp_path, monkeypatch):
    """The handler never actually fixes anything -> command keeps failing ->
    the SAME max_iterations budget that bounds ordinary retries also bounds
    this fail/handler/retry cycle. No new counter; this proves it."""
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="false", on_fail="step-fix"),
        _step("Fix tests", id="step-fix"),
    ])
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 6\n[notify]\nwait_for_reply = false\n")
    fake = RecordingAdapter(texts=["still broken"] * 10)   # handler never fixes it
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)   # must return, not hang
    assert len(fake.calls) >= 2   # handler was retried more than once before the budget ran out
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-t1"]["status"] == "failed"   # command never got to "ok"


def test_checkpoint_captured_for_command_step(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Tests", id="step-t1", type="command", command="true"),
    ])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    fresh = tasks.find(env, t.id)
    assert "step-t1" in fresh.stage_checkpoints
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_command_steps.py -q`
Expected: FAIL (`run()` has no command-step branch yet).

- [ ] **Step 3: Implement per the Interfaces section above.** Read the CURRENT `run()` body in `harn/loop.py` (around line 1104-1109, the `if pending:` block) before editing — insert the command-step branch using the exact `sid`/`title`/`step` variable names already in scope there. Add `_run_command_step` and `_run_onfail_handler` as new module-level functions placed near `_checkpoint_stage` (logical grouping — both are step-execution helpers). Add the `onfail_context` parameter to `_build_step_prompt`'s signature (default `""`) and one `if onfail_context: parts.append(onfail_context)` line in its body, placed as described in step 3 of the Interfaces section.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_command_steps.py -q`
Expected: all 5 pass.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q 2>&1 | tail -10`
Expected: fully green — this task only ADDS a new branch gated on `step.get("type") == "command"`, which is always `""` for every existing test's steps (Task 1 defaults it to `""`), so no existing agent-step test path changes behavior.

- [ ] **Step 6: Commit**

```bash
git add harn/loop.py tests/test_command_steps.py
git commit -m "feat(loop): command steps + on-fail handler dispatch (Phase 2)"
```

---

### Task 3: `run_step()` support for command steps (manual Run/Rerun)

**Files:**
- Modify: `harn/loop.py`
- Test: `tests/test_run_step.py`

**Interfaces:**
- Consumes: Task 2's `_run_command_step` (reused, not reimplemented).
- Produces: `run_step()` (the studio UI's per-step Run/Rerun and `harn run --task ID --step STEP_ID` entry point) branches on `step.get("type") == "command"` the same way `run()` does, calling `_run_command_step` instead of the agent-turn code, and returns `{"ok": bool, "step_id": ..., "title": ..., "output": <tail>}` for command steps (no `"text"` key — there's no agent text) versus the EXISTING `{"ok": ..., "step_id": ..., "title": ..., "text": ...}` shape for agent steps (leave the agent-step return shape untouched — don't add an `output` key to it, and don't add a `text` key to the command-step return, so callers can distinguish step type from the response shape if needed, though the primary signal is still `step["type"]`).

Note: `_run_command_step` as written in Task 2 needs the full `steps` list (for on_fail handler lookup) — `run_step()` already loads `plan["nodes"]` via `workflows.load_task_plan`, so pass `[n for n in plan["nodes"] if n.get("kind") == "step"]` as the `steps` argument (matching how `run()` builds its own `steps` list, for consistency, though `run_step` doesn't need to filter by `enabled` since the user explicitly picked this exact step to run).

- [ ] **Step 1: Write the failing tests.** Add to `tests/test_run_step.py` (find the existing test file from Phase 1's Task 5 — these tests follow its established `_repo`/git-fixture pattern; read the file first to match its exact helper names before writing):

```python
def test_run_step_executes_command_type_without_agent_call(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"] = [{"kind": "step", "title": "Tests", "id": "step-cmd1",
                      "type": "command", "command": "true", "on_fail": "",
                      "agent": "", "model": "", "effort": "", "temperature": "",
                      "required": [], "tools": [], "enabled": True}]
    workflows.save_task_plan(env, t.id, plan)
    fake_calls = []
    monkeypatch.setattr(loop, "get_adapter",
                        lambda n: (_ for _ in ()).throw(AssertionError("no agent for command step")))
    r = loop.run_step(root, env, t.id, "step-cmd1")
    assert r["ok"] is True
    assert "text" not in r
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["step-cmd1"]["status"] == "ok"


def test_run_step_command_rerun_restores_checkpoint(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    env = _env(root)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"] = [{"kind": "step", "title": "Touch", "id": "step-cmd2",
                      "type": "command", "command": f"sh -c 'echo x >> {root}/marker.txt'",
                      "on_fail": "", "agent": "", "model": "", "effort": "",
                      "temperature": "", "required": [], "tools": [], "enabled": True}]
    workflows.save_task_plan(env, t.id, plan)
    loop.run_step(root, env, t.id, "step-cmd2")
    loop.run_step(root, env, t.id, "step-cmd2", rerun=True)
    # both runs append (checkpoint restores the WORKING TREE, not undo the
    # command's own side effects outside version control) — assert it ran twice
    assert (root / "marker.txt").read_text().count("x") == 2
```

(The second test documents an important, deliberate limitation: checkpoint/rerun restores the git-tracked working tree, not arbitrary side effects a shell command had outside it. This is inherent to the git-stash-based checkpoint mechanism from Phase 1, not something Phase 2 needs to solve — the test exists to make that limitation explicit and regression-tested, not to fix it.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_run_step.py -q`
Expected: the two new tests FAIL.

- [ ] **Step 3: Implement.** In `run_step()`, after `step = next(...)` / `title = step.get("title", "")` (existing lines), insert:

```python
if step.get("type") == "command":
    plan_steps = [n for n in plan["nodes"] if n.get("kind") == "step"]
    if rerun:
        ref = task.stage_checkpoints.get(step_id)
        if ref:
            gitutil.rollback_to(ref, project_root, apply=True)
    outcome = _run_command_step(env_dir, project_root, task, step, plan_steps)
    fresh = tasks.find(env_dir, task_id) or task
    entry = fresh.step_results.get(step_id, {})
    return {"ok": entry.get("status") == "ok", "step_id": step_id, "title": title,
            "output": entry.get("output", "")}
```

Place this BEFORE the existing `if rerun:` block for the agent-step path (the existing rerun-restore code stays for agent steps; the command branch above has its OWN rerun-restore since it returns early and must not fall through to the agent-turn code below it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_run_step.py -q`
Expected: all pass (existing + 2 new).

- [ ] **Step 5: Run the full suite + JS syntax check** (unaffected by this Python-only task, confirm nothing broke):

```bash
python3 -m pytest -q 2>&1 | tail -10
python3 -c "
import re
text = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', text, re.S)
open('/tmp/studio_check.js','w').write(m.group(1))
"
node --check /tmp/studio_check.js
```
Expected: full suite green; `node --check` exit 0.

- [ ] **Step 6: Commit**

```bash
git add harn/loop.py tests/test_run_step.py
git commit -m "feat(loop): run_step supports command-type steps"
```

---

### Task 4: Studio UI — Type toggle, Command textarea, On-fail dropdown

**Files:**
- Modify: `harn/studio.py`
- Test: live browser verification via Claude Preview MCP (no new Python tests — this is pure frontend JS + one backend field pass-through that already works generically, see below).

**Interfaces:**
- Backend: NONE needed. `task_plan_payload`/`save_task_plan_route` (from Phase 1) already round-trip the full node dict verbatim (they don't enumerate fields) — `type`/`command`/`on_fail` pass through automatically once the frontend sets them on `selNode`. `models_payload`/`/api/workflow` (preset save) are equally field-agnostic. Confirm this claim in Step 1 before writing any frontend code — if it's wrong, that changes this task's scope.
- Frontend: `renderInsp()`'s `isStep` block gets a Type toggle ABOVE the existing `modelSection` conditional. `Type: Agent` (default, `n.type === '' || n.type === 'agent'`) renders the EXISTING `modelSection` unchanged. `Type: Command` renders a NEW block: a `<textarea>` bound to `n.command` (via `setStepField('command', this.value)` — already generic, no change needed) and an `<select>` "On fail →" listing every OTHER step in `S.workflow.nodes` by title, value = that step's id, bound to `n.on_fail` via `setStepField('on_fail', this.value)`.

- [ ] **Step 1: Confirm the backend pass-through claim.** Read `task_plan_payload`/`save_task_plan_route` in `harn/studio.py` (`grep -n "def task_plan_payload\|def save_task_plan_route" harn/studio.py`) and `workflows.save_task_plan`/`load_task_plan` in `harn/workflows.py`. Confirm none of them enumerate or filter node dict keys (they should just pass the parsed JSON through to `workflow.ensure_ids`/`json.dump` and back). If you find a place that DOES enumerate fields (e.g. an allowlist), note it in your report and extend it — this is a real possible gap, don't assume.

- [ ] **Step 2: Add the Type toggle + Command block.** In `renderInsp()`, locate the `const modelSection=isStep?(()=>{...})():'';` block (added in Phase 1). Immediately before it, add:

```javascript
const stepType=(n.type==='command')?'command':'agent';
const typeToggle=isStep?`
  <label>Type</label>
  <select onchange="setStepField('type',this.value==='agent'?'':this.value)">
    <option value="agent" ${stepType==='agent'?'selected':''}>Agent — an LLM turn</option>
    <option value="command" ${stepType==='command'?'selected':''}>Command — a shell command, no LLM</option>
  </select>` : '';
const commandSection=(isStep && stepType==='command')?(()=>{
  const others=S.workflow.nodes.filter(x=>x.kind==='step' && x!==n && x.id);
  const onFailOpts=`<option value="">(none — just record the failure)</option>`
    + others.map(x=>`<option value="${esc(x.id)}" ${n.on_fail===x.id?'selected':''}>${esc(x.title)}</option>`).join('');
  const taskId=runStepTaskId();
  const busy=!!BOARD.run;
  const runBtns=n.id?`
  <div class="row" style="margin-top:8px;gap:10px">
    <button ${busy||!taskId?'disabled':''} title="${taskId?'Run just this step for '+esc(taskId):'No task selected'}" onclick="runStep('${esc(n.id)}')">▶ Run step</button>
    <button ${busy||!taskId?'disabled':''} title="${taskId?'Rerun this step for '+esc(taskId):'No task selected'}" onclick="rerunStep('${esc(n.id)}')">↻ Rerun step</button>
  </div>` : '';
  return `
  <label>Command <span class="mut">(shell, runs in the project root — no LLM, no tokens)</span></label>
  <textarea style="min-height:70px;font-family:ui-monospace,Menlo,monospace;font-size:12.5px"
    oninput="setStepField('command',this.value)" placeholder="npm test">${esc(n.command||'')}</textarea>
  <label>On fail <span class="mut">(dispatch this agent step, then retry the command)</span></label>
  <select onchange="setStepField('on_fail',this.value)">${onFailOpts}</select>
  ${runBtns}
  <div class="mut" style="font-size:11px;margin-top:6px;line-height:1.6">
    Failure = non-zero exit or timeout. With no On-fail target, a failed
    command step just records its output for the next step to see — same as
    today's default behavior.
  </div>`;
})():'';
```

Then change the final template literal's `${modelSection}` reference (search for where `modelSection` is interpolated into the returned HTML string — near the end of `renderInsp()`) to:

```javascript
${typeToggle}${stepType==='agent'?modelSection:commandSection}
```

- [ ] **Step 3: Default new steps to `type: ''`.** In `addStep()`, add `type:'',command:'',on_fail:''` to the new-node literal (alongside the existing `id:'',agent:'',...`):

```javascript
const n={title:uniqueTitle('New step'),body:'',required:[],tools:[],kind:'step',enabled:true,
  id:'',agent:'',model:'',effort:'',temperature:'',type:'',command:'',on_fail:''};
```

- [ ] **Step 4: JS syntax check**

```bash
python3 -c "
import re
text = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', text, re.S)
open('/tmp/studio_check.js','w').write(m.group(1))
"
node --check /tmp/studio_check.js
```
Expected: exit 0.

- [ ] **Step 5: Full pytest** — `python3 -m pytest -q` → green (this task shouldn't change any Python test outcome — it's JS-only plus the Step 1 verification pass).

- [ ] **Step 6: Live browser verification.** Repo pattern (used throughout this project's session history): scratch project in `/tmp`, `python3 -m harn.cli setup <path> --no-install --no-onboard`, temp `.claude/launch.json` running `python3 -m harn.cli ui <path> --port 8765 --no-open`, drive via the Claude Preview MCP tools (`preview_start`/`preview_eval`/`preview_screenshot`/`preview_network`/`preview_stop`). Verify:
  1. Selecting a step, switching Type to "Command" replaces the Agent/Model block with a Command textarea + On-fail dropdown; switching back to "Agent" restores the original block with the step's agent/model VALUES intact (not cleared).
  2. The On-fail dropdown does NOT list the currently-selected step itself, and DOES list every other step by title.
  3. Setting a command + on_fail target, saving (`saveFlow()`), then re-parsing the saved WORKFLOW.md server-side (`python3 -c "from harn import workflow; ..."` in the same scratch project) shows `Type: command`, `Command: ...`, and `On fail: <target's title>` lines present.
  4. Renaming the on_fail target step and re-saving: the `On fail:` line in the composed file now shows the NEW title (server-side check, same technique as #3).
  Clean up: stop the preview server, remove the scratch project and temp `.claude/launch.json`.

- [ ] **Step 7: Commit**

```bash
git add harn/studio.py
git commit -m "feat(studio): Type toggle (Agent/Command) + Command textarea + On-fail dropdown"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md` (only if it documents the per-step field set from Phase 1 — check first)

**Interfaces:** none new — pure documentation.

- [ ] **Step 1: Check for a Phase-1 field reference in README.md**

```bash
grep -n "Agent:\|Model:\|Effort:\|Temperature:\|per-step" README.md
```

If nothing found, this task is a no-op — report that and stop (don't invent a new README section unprompted; Phase 1's Task 7 established the precedent of only touching docs that already had stale content).

- [ ] **Step 2: If a per-step field reference exists, add `Type:`/`Command:`/`On fail:` to it** in the same terse style as the surrounding text — one or two lines, not a new subsection, matching how Phase 1's equivalent fields were (or weren't) documented.

- [ ] **Step 3: Full suite + JS check** (should be unaffected — confirm nothing broke):

```bash
python3 -m pytest -q 2>&1 | tail -5
```

- [ ] **Step 4: Commit** (only if Step 2 made a change; otherwise skip this task's commit entirely and say so in the report)

```bash
git add README.md
git commit -m "docs: document command steps + on-fail (Phase 2)"
```
