# Step Observability + Required/Recommended Skills & Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `harn run`/studio users: a per-step activity log, a way to preview/export the exact prompt a step will run with, a `recommended`-vs-`required` tier for Skills/Tools with post-step usage badges and retry-once-then-BLOCKED enforcement, a clearly-labeled Pause/Resume UI, and the ability to answer a BLOCKED question directly from studio (with the existing Telegram escalation left untouched).

**Architecture:** Extends the existing per-step ledger (`task.step_results`), the existing `state.State`/`events.jsonl` machinery, and the existing `harn/studio.py` single-file UI — no new subsystems, no new dependencies. The one new cross-cutting piece is a central "tag every MCP tool call" wrapper around `mcp.tool()` in `harn/mcp_server.py`, added once instead of touching each of the 35 individual tool functions.

**Tech Stack:** Python stdlib only (dataclasses, `re`, `http.server`), vanilla JS in `harn/studio.py`'s inline `<script>` block, pytest.

## Global Constraints

- Every committed change bumps `__version__` in `harn/__init__.py` AND the version in `pyproject.toml` together (standing project rule — current version is 0.17.9; this plan's tasks bump it sequentially, 0.17.10, 0.17.11, ... one bump per task's commit).
- harn NEVER creates git commits on the user's behalf, anywhere.
- Backward compatibility: every existing WORKFLOW.md (`Skills (required: a, b)` with no `recommended:`, bare `Tools: a, b`) must keep parsing EXACTLY as it does today — this phase only adds an optional suffix/tier, never changes existing meaning.
- Required-skill/tool enforcement (retry-once, then BLOCKED) applies to SEQUENTIAL steps only — per the spec's explicit Non-goal, a step running inside a Phase-3 parallel wave still records usage DATA (so studio badges render correctly) but never triggers the automatic retry/BLOCKED cycle, because `state.State` is a single shared file and cannot represent N concurrently-active steps.
- No new third-party dependencies; follow each file's existing style exactly (stdlib-only Python, vanilla JS, no build step for studio.py).
- Telegram escalation timing (`_telegram_wait`/`_await_answer`/`Config.chat_grace_minutes`) is NOT touched by this plan — it already posts the full question text (including the agent's embedded recommendation) to Telegram after the grace period. This plan only adds a studio-side path to answer that question earlier, without leaving the UI.

---

### Task 1: `workflow.py` — `recommended:` tier for Skills and Tools

**Files:**
- Modify: `harn/workflow.py:37-53` (regex definitions), `harn/workflow.py:189` (`_TOOLS_RE`), `harn/workflow.py:255-348` (`parse()`), `harn/workflow.py:351-398` (`compose()`)
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: nothing new — this task is self-contained within `workflow.py`.
- Produces: parsed step nodes gain two new keys, `skills_recommended: list[str]` (default `[]`) and `tools_recommended: list[str]` (default `[]`), alongside the existing `required: list[str]` and `tools: list[str]`. Later tasks (5, 6, 7) read these four fields by these exact names.

Today, `harn/workflow.py:37` has:
```python
_REQ_RE = re.compile(r"^\s*Skills\s*\(required:\s*([^)]*)\)\s*$", re.IGNORECASE)
```
and `harn/workflow.py:189` has:
```python
_TOOLS_RE = re.compile(r"^\s*Tools:\s*(.*)$", re.IGNORECASE)
```
Neither captures a `recommended:` clause. This task extends both to optionally capture one, and extends `parse()`/`compose()` to round-trip the new fields.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workflow.py`:

```python
def test_skills_recommended_tier_parses(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env.parent / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards; recommended: testing, ui)\n"
        "Tools (required: run_tests; recommended: read_design)\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    node = parsed["nodes"][0]
    assert node["required"] == ["standards"]
    assert node["skills_recommended"] == ["testing", "ui"]
    assert node["tools"] == ["run_tests"]
    assert node["tools_recommended"] == ["read_design"]


def test_old_bare_tools_line_still_parses_as_all_recommended(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env.parent / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards)\n"
        "Tools: run_tests, read_design\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    node = parsed["nodes"][0]
    assert node["tools"] == ["run_tests", "read_design"]
    assert node["tools_recommended"] == []
    assert node["skills_recommended"] == []


def test_old_skills_required_with_no_recommended_still_parses(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env.parent / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards, constraints)\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    node = parsed["nodes"][0]
    assert node["required"] == ["standards", "constraints"]
    assert node["skills_recommended"] == []


def test_recommended_tier_round_trips_through_compose(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (env.parent / "WORKFLOW.md").write_text(
        "## 1. Implement\n"
        "Skills (required: standards; recommended: testing)\n"
        "Tools (required: run_tests; recommended: read_design)\n",
        encoding="utf-8")
    parsed = workflow.parse(env)
    composed = workflow.compose(env, parsed)
    reparsed_dir = tmp_path / "harn_env2"
    reparsed_dir.mkdir()
    (reparsed_dir.parent / "WORKFLOW.md").write_text(composed, encoding="utf-8")
    reparsed = workflow.parse(reparsed_dir)
    node = reparsed["nodes"][0]
    assert node["required"] == ["standards"]
    assert node["skills_recommended"] == ["testing"]
    assert node["tools"] == ["run_tests"]
    assert node["tools_recommended"] == ["read_design"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_workflow.py -k recommended -v`
Expected: FAIL — `KeyError: 'skills_recommended'` (the key doesn't exist yet).

- [ ] **Step 3: Extend the regexes**

In `harn/workflow.py`, replace the `_REQ_RE` definition (currently line 37):

```python
_REQ_RE = re.compile(
    r"^\s*Skills\s*\(required:\s*([^;)]*)(?:;\s*recommended:\s*([^)]*))?\)\s*$",
    re.IGNORECASE)
```

Replace the `_TOOLS_RE` definition (currently line 189):

```python
_TOOLS_RE = re.compile(
    r"^\s*Tools\s*(?:\(required:\s*([^;)]*)(?:;\s*recommended:\s*([^)]*))?\)|:\s*(.*))$",
    re.IGNORECASE)
```

`_TOOLS_RE` now matches EITHER the old bare `Tools: a, b` form (captured in group 3) OR the new `Tools (required: a; recommended: b)` form (groups 1/2) — both alternatives share one compiled regex so a single `_TOOLS_RE.match(line)` check still works everywhere it's already called.

- [ ] **Step 4: Update the node-init dict in `parse()`**

In `harn/workflow.py`'s `parse()` function, find the node-init dict (currently around line 269):

```python
            cur = {"title": (m.group(2).strip() if m else raw),
                   "required": [], "tools": [], "enabled": True,
                   "id": "", "agent": "", "model": "", "effort": "",
                   "temperature": "", "type": "", "command": "", "on_fail": "",
                   "parallel": "",
                   "_num": bool(m), "_decl": False}
```

Add the two new keys:

```python
            cur = {"title": (m.group(2).strip() if m else raw),
                   "required": [], "skills_recommended": [],
                   "tools": [], "tools_recommended": [], "enabled": True,
                   "id": "", "agent": "", "model": "", "effort": "",
                   "temperature": "", "type": "", "command": "", "on_fail": "",
                   "parallel": "",
                   "_num": bool(m), "_decl": False}
```

- [ ] **Step 5: Update the parsing blocks for `req`/`tl` in `parse()`**

Replace the existing `req = _REQ_RE.search(line)` block (currently around line 282):

```python
        req = _REQ_RE.search(line)
        if req:
            cur["_decl"] = True
            cur["required"] = [s.strip() for s in req.group(1).split(",")
                               if re.fullmatch(r"[a-z0-9_-]+", s.strip())]
            continue
```

with:

```python
        req = _REQ_RE.search(line)
        if req:
            cur["_decl"] = True
            cur["required"] = [s.strip() for s in req.group(1).split(",")
                               if re.fullmatch(r"[a-z0-9_-]+", s.strip())]
            rec_raw = req.group(2) or ""
            cur["skills_recommended"] = [s.strip() for s in rec_raw.split(",")
                                         if re.fullmatch(r"[a-z0-9_-]+", s.strip())]
            continue
```

Replace the existing `tl = _TOOLS_RE.match(line)` block (currently around line 288):

```python
        tl = _TOOLS_RE.match(line)
        if tl:
            cur["_decl"] = True
            cur["tools"] = [t.strip() for t in tl.group(1).split(",") if t.strip()]
            continue
```

with:

```python
        tl = _TOOLS_RE.match(line)
        if tl:
            cur["_decl"] = True
            if tl.group(3) is not None:
                # old bare "Tools: a, b" form — sugar for all-recommended, none required
                cur["tools_recommended"] = [t.strip() for t in tl.group(3).split(",")
                                            if t.strip()]
                cur["tools"] = []
            else:
                cur["tools"] = [t.strip() for t in (tl.group(1) or "").split(",")
                                if t.strip()]
                cur["tools_recommended"] = [t.strip() for t in (tl.group(2) or "").split(",")
                                            if t.strip()]
            continue
```

- [ ] **Step 6: Update `compose()` to write both tiers back out**

Find the Skills-line composition (currently around line 378):

```python
        if kind == "step":
            req = [s for s in n.get("required", []) if s]
            out.append(f"Skills (required: {', '.join(req)})" if req
                       else "Skills (required: )")
        tools = [t for t in n.get("tools", []) if t]
        if tools:
            out.append(f"Tools: {', '.join(tools)}")
```

Replace with:

```python
        if kind == "step":
            req = [s for s in n.get("required", []) if s]
            rec = [s for s in n.get("skills_recommended", []) if s]
            skills_line = f"Skills (required: {', '.join(req)}" if req else "Skills (required:"
            if rec:
                skills_line += f"; recommended: {', '.join(rec)}"
            skills_line += ")" if req or rec else " )"
            out.append(skills_line)
        tools = [t for t in n.get("tools", []) if t]
        tools_rec = [t for t in n.get("tools_recommended", []) if t]
        if tools or tools_rec:
            if tools:
                tools_line = f"Tools (required: {', '.join(tools)}"
                if tools_rec:
                    tools_line += f"; recommended: {', '.join(tools_rec)}"
                tools_line += ")"
                out.append(tools_line)
            else:
                out.append(f"Tools: {', '.join(tools_rec)}")
```

(The `Skills (required: )` fallback preserves today's exact output for a step with no skills declared at all — confirm this against the existing `test_workflow.py` round-trip tests for steps with empty `required`.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_workflow.py -v`
Expected: PASS — all existing `test_workflow.py` tests plus the 4 new ones.

- [ ] **Step 8: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 475 (baseline) + 4 = 479 passed. If any pre-existing test in `tests/test_workflow.py` or `tests/test_gitutil_worktree.py`/`tests/test_parallel_waves.py` fails, it means the old bare-`Tools:` regex alternative broke — re-check `_TOOLS_RE`'s three-way alternation before proceeding.

- [ ] **Step 9: Bump version and commit**

Edit `harn/__init__.py`: change `__version__ = "0.17.9"` to `__version__ = "0.17.10"`.
Edit `pyproject.toml`: change `version = "0.17.9"` to `version = "0.17.10"`.

```bash
git add harn/workflow.py harn/__init__.py pyproject.toml tests/test_workflow.py
git commit -m "feat(workflow): recommended: tier for Skills/Tools lines (Phase 4); version 0.17.10"
```

---

### Task 2: `loop.py` — capture agent-turn step output in the ledger

**Files:**
- Modify: `harn/loop.py:1828` (the sequential-step "ok" ledger write in `run()`)
- Test: `tests/test_run_step.py` or `tests/test_events.py` (whichever already has a `run()`-driving fixture — check both, extend the closer match)

**Interfaces:**
- Consumes: nothing new.
- Produces: `task.step_results[sid]["output"]` now exists for AGENT-turn steps too (previously only command-type steps at `harn/loop.py:619`/`624` had this key). Task 7 (studio per-step log) reads `step_results[sid]["output"]` uniformly for both step types.

Today, `harn/loop.py:1828` (the sequential agent-turn "step done" ledger write inside `run()`) is:

```python
                task.step_results[sid] = {"status": "ok", "started": started,
                                          "ended": tasks._now_iso(),
                                          "tokens": result.total_tokens}
```

It has no `output` key, unlike the command-step path at `harn/loop.py:618-619`:
```python
        task.step_results[sid] = {"status": "ok", "started": started,
                                  "ended": ended, "output": result.tail(40)}
```

- [ ] **Step 1: Write the failing test**

Add to `tests/test_run_step.py` (check the file's existing imports/fixtures first — reuse whatever fake-adapter/tmp-env fixture the file already has, e.g. `_env`/`_adapter` helpers already used by neighboring tests in that file):

```python
def test_sequential_agent_step_records_output_in_ledger(tmp_path):
    env, project_root = _env(tmp_path)  # use this file's existing env-setup helper
    task = tasks.create_task(env, "Do the thing")
    workflow.snapshot_for_task(env, task.id, [
        {"title": "Only step", "kind": "step", "id": "s1", "required": [],
         "tools": [], "enabled": True},
    ])
    cfg = Config()
    loop.run(project_root, env, max_iterations=1, cfg=cfg,
             adapter=_FakeAdapter(text="did the work", ok=True))
    fresh = tasks.find(env, task.id)
    assert fresh.step_results["s1"]["status"] == "ok"
    assert "did the work" in fresh.step_results["s1"]["output"]
```

(Adjust `_FakeAdapter`'s exact constructor/kwarg names to match whatever fake adapter class `tests/test_run_step.py` already defines and uses in its other tests — read the top of that file before writing this test to match its exact existing signature rather than guessing new kwargs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_run_step.py -k records_output -v`
Expected: FAIL with `KeyError: 'output'`.

- [ ] **Step 3: Implement**

In `harn/loop.py`, replace the block at line 1828:

```python
                task.step_results[sid] = {"status": "ok", "started": started,
                                          "ended": tasks._now_iso(),
                                          "tokens": result.total_tokens}
```

with:

```python
                task.step_results[sid] = {"status": "ok", "started": started,
                                          "ended": tasks._now_iso(),
                                          "tokens": result.total_tokens,
                                          "output": (result.text or "")[-4000:]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_run_step.py -k records_output -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 479 (Task 1's count) + 1 = 480 passed.

- [ ] **Step 6: Bump version and commit**

Edit `harn/__init__.py`: `0.17.10` → `0.17.11`. Edit `pyproject.toml` to match.

```bash
git add harn/loop.py harn/__init__.py pyproject.toml tests/test_run_step.py
git commit -m "feat(loop): record agent-turn step output in the ledger, not just command steps; version 0.17.11"
```

---

### Task 3: `loop.py` + `studio.py` — full-context preview/export

**Files:**
- Modify: `harn/loop.py` (add `preview_step_prompt` near `_build_step_prompt` at line 505; add `save_context_export` near it)
- Modify: `harn/studio.py` (add two backend routes + inspector "View full context"/"Copy to file" UI)
- Test: `tests/test_loop_preview.py` (new file) and a studio-side JS syntax check

**Interfaces:**
- Consumes: `harn/loop.py`'s existing `_build_step_prompt(env_dir, cfg, task, step, feedback_tail="", auto=False, onfail_context="", parallel_note="") -> str` (unchanged signature).
- Produces: `loop.preview_step_prompt(env_dir: Path, cfg: Config, task: tasks.Task, step: dict) -> str` and `loop.save_context_export(env_dir: Path, task_id: str, step_id: str, text: str) -> Path`. Studio routes `GET /api/task_plan/step_prompt?task=<id>&step=<id>` and `POST /api/task_plan/step_prompt/export` (body `{"task":id,"step":id,"text":str}`) that later tasks' UI doesn't depend on, but which the "View full context" button (this task) wires up end-to-end.

- [ ] **Step 1: Write the failing test**

Create `tests/test_loop_preview.py`:

```python
"""Tests for loop.preview_step_prompt / loop.save_context_export (Phase 4)."""
from pathlib import Path

from harn import loop, tasks, workflow
from harn.config import Config


def _make_env(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    return env, project_root


def test_preview_step_prompt_matches_a_real_turns_prompt(tmp_path, monkeypatch):
    env, project_root = _make_env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    step = {"title": "Only step", "id": "s1", "body": "do it well",
            "required": [], "tools": [], "enabled": True}
    cfg = Config()

    captured = {}

    def fake_run_turn(adapter, env_dir, prompt, project_root, **kw):
        captured["prompt"] = prompt
        class R:
            ok = True
            text = "done"
            total_tokens = 10
            def tail(self, n): return "done"
            def usage_str(self): return ""
        return R()

    monkeypatch.setattr(loop, "_run_turn", fake_run_turn)
    preview = loop.preview_step_prompt(env, cfg, task, step)
    # No turn has actually run — no ledger entry should exist yet.
    fresh = tasks.find(env, task.id)
    assert "s1" not in fresh.step_results
    # The preview must be byte-identical to what _build_step_prompt itself produces.
    assert preview == loop._build_step_prompt(env, cfg, task, step)


def test_save_context_export_writes_a_file_and_returns_its_path(tmp_path):
    env, _ = _make_env(tmp_path)
    p = loop.save_context_export(env, "task-1", "s1", "the full prompt text")
    assert p.exists()
    assert p.read_text(encoding="utf-8") == "the full prompt text"
    assert p.parent == env / "state" / "context_exports"
    assert "task-1" in p.name and "s1" in p.name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_loop_preview.py -v`
Expected: FAIL with `AttributeError: module 'harn.loop' has no attribute 'preview_step_prompt'`.

- [ ] **Step 3: Implement `preview_step_prompt` and `save_context_export`**

In `harn/loop.py`, immediately after the `_build_step_prompt` function (which ends at line 560, right before `_checkpoint_stage` at line 563), add:

```python
def preview_step_prompt(env_dir: Path, cfg: Config, task: "tasks.Task",
                        step: dict) -> str:
    """Return the EXACT prompt a real turn for this step would receive, with
    zero side effects (no turn is run, no event emitted, no ledger touched).
    Powers studio's "View full context" button and the `harn` CLI's future
    preview command — both need to show a human the same text the agent
    will actually see, before or after the fact."""
    return _build_step_prompt(env_dir, cfg, task, step)


def save_context_export(env_dir: Path, task_id: str, step_id: str,
                        text: str) -> Path:
    """Write a previewed/inspected step prompt to a plain text file a human
    can download or open directly, per the "even copy it to a separate
    file" requirement. Filename includes a timestamp so repeated exports of
    the same step never collide."""
    out_dir = env_dir / "state" / "context_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = tasks._now_iso().replace(":", "-").replace(".", "-")
    p = out_dir / f"{task_id}_{step_id}_{stamp}.txt"
    p.write_text(text, encoding="utf-8")
    return p
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_loop_preview.py -v`
Expected: PASS.

- [ ] **Step 5: Add the two studio backend routes**

In `harn/studio.py`, find the `do_GET` method's route dispatch (currently ending at line 663 with the `else:` 404 fallback, right after the `/api/attachments/file` branch at line 656-662). Add a new branch before the final `else:`:

```python
            elif route == "/api/task_plan/step_prompt":
                task_id, step_id = self._query("task"), self._query("step")
                self._json(step_prompt_payload(env, task_id or "", step_id or ""))
```

Find the `do_POST` method's route dispatch (currently ending around line 708-710 with `/api/defaults`). Add a new branch:

```python
            elif route == "/api/task_plan/step_prompt/export":
                body = self._read_json()
                self._json(step_prompt_export_payload(
                    env, body.get("task", ""), body.get("step", ""),
                    body.get("text", "")))
```

Now add the two payload-builder functions. Find where other `*_payload(env, ...)` functions live in `harn/studio.py` (e.g. `task_plan_payload` — grep for `def task_plan_payload` to find the right neighborhood) and add these next to it:

```python
def step_prompt_payload(env_dir: Path, task_id: str, step_id: str) -> dict:
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"error": f"no such task: {task_id}"}
    plan = workflow_mod.load_for_task(env_dir, task_id)
    step = next((s for s in plan.get("nodes", []) if s.get("id") == step_id), None)
    if step is None:
        return {"error": f"no such step: {step_id}"}
    cfg = config_mod.load(env_dir.parent)
    text = loop_mod.preview_step_prompt(env_dir, cfg, task, step)
    return {"prompt": text}


def step_prompt_export_payload(env_dir: Path, task_id: str, step_id: str,
                                text: str) -> dict:
    if not text.strip():
        return {"error": "nothing to export"}
    path = loop_mod.save_context_export(env_dir, task_id, step_id, text)
    return {"path": str(path), "name": path.name}
```

Check the exact existing import aliases at the top of `harn/studio.py` (grep for `import.*as loop_mod\|import.*as tasks_mod\|import.*as workflow_mod\|import.*as config_mod` — if `harn/studio.py` imports these modules under different names, e.g. plain `from . import loop` without an alias, use THAT existing name instead of inventing `loop_mod` — match the file's real import style exactly, don't guess).

Also check `workflow.py` for the actual name of the per-task snapshot loader (the plan calls it `workflow.load_for_task` above as a placeholder for whichever function already loads a task's own `.workflow.json` snapshot — grep `harn/workflow.py` and `harn/studio.py` for `snapshot_for_task`/`load_for_task`/`task_plan_payload`'s own internals to find the real function name and use it verbatim).

- [ ] **Step 6: Add the "View full context" button and panel to the step inspector**

Find `renderInsp()` in `harn/studio.py`'s `<script>` block (grep `function renderInsp` or the flow-tab step inspector rendering — Task 7 of Phase 3's report referenced "the step inspector" living in the same rendering path as the parallel-group note, so look for where that note (`"part of parallel group..."`) was inserted and add this near it). Add a button:

```html
<button class="ghost" onclick="viewFullContext('${esc(n.id)}')">View full context</button>
```

Add the JS function (near other `async function ...` helpers that call `fetch(api(...))`, e.g. near `pollProgress`):

```javascript
async function viewFullContext(stepId){
  const taskId = boardSel || (S.currentTaskId||'');
  if(!taskId){ alert('Select a task first.'); return; }
  const r = await (await fetch(api(`/api/task_plan/step_prompt?task=${encodeURIComponent(taskId)}&step=${encodeURIComponent(stepId)}`))).json();
  if(r.error){ alert(r.error); return; }
  const w = window.open('', '_blank');
  w.document.title = 'Full context — ' + stepId;
  w.document.body.style.cssText = 'white-space:pre-wrap;font-family:monospace;padding:16px;';
  w.document.body.textContent = r.prompt;
  const btn = w.document.createElement('button');
  btn.textContent = 'Copy to file';
  btn.style.cssText = 'position:fixed;top:8px;right:8px;';
  btn.onclick = async () => {
    const rr = await fetch(api('/api/task_plan/step_prompt/export'), {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({task: taskId, step: stepId, text: r.prompt})});
    const j = await rr.json();
    if(j.error) alert(j.error); else alert('Saved to ' + j.path);
  };
  w.document.body.prepend(btn);
}
```

(Confirm `boardSel`/`S.currentTaskId` — use whichever variable the existing board/flow tab actually tracks as "the currently selected task id"; grep for how `renderTaskDetail()`/`launch_step` calls already identify the current task and reuse that exact variable name, don't invent a new one.)

- [ ] **Step 7: Verify JS syntax**

Run the project's existing script-extraction check (the same one used in Phase 3 Task 7's report — extract the `<script>...</script>` block from `harn/studio.py` to a temp `.js` file and run `node --check` on it):

```bash
python3 -c "
import re
html = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', html, re.DOTALL)
open('/tmp/studio_check.js', 'w').write(m.group(1))
"
node --check /tmp/studio_check.js
```
Expected: no output (syntax OK).

- [ ] **Step 8: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 480 (prior count) + 2 = 482 passed.

- [ ] **Step 9: Bump version and commit**

Edit `harn/__init__.py`: `0.17.11` → `0.17.12`. Edit `pyproject.toml` to match.

```bash
git add harn/loop.py harn/studio.py harn/__init__.py pyproject.toml tests/test_loop_preview.py
git commit -m "feat: preview + export a step's full prompt from studio (Phase 4); version 0.17.12"
```

---

### Task 4: `state.py` — `current_step` field

**Files:**
- Modify: `harn/state.py:22-29` (the `State` dataclass)
- Modify: `harn/loop.py` (set/clear `current_step` around a SEQUENTIAL step's turn only — inside `run()`'s main step-walk, around line 1769-1832; NOT inside `_run_parallel_wave`, per the Non-goal)
- Test: `tests/test_state.py` (check whether this file exists; if not, add tests to whichever file already tests `state.py` — grep for `current_task` round-trip tests to find it)

**Interfaces:**
- Consumes: `state.State`'s existing `save`/`load` (unchanged signatures).
- Produces: `State.current_step: str | None = None` (new field, mirrors `current_task`). Task 5 (tool-usage tagging) and Task 6 (enforcement) both read `state.State.load(state_dir).current_step` to scope a `tool_used`/`context_read` event to the step currently running.

- [ ] **Step 1: Write the failing test**

Add to whichever test file already covers `state.py` (e.g. `tests/test_state.py` — grep `from harn import state` across `tests/` to find the right file, or create `tests/test_state.py` if none exists):

```python
def test_current_step_round_trips_through_save_load(tmp_path):
    state_dir = tmp_path / "state"
    st = state.State(current_task="t1", current_step="s2")
    st.save(state_dir)
    reloaded = state.State.load(state_dir)
    assert reloaded.current_step == "s2"


def test_current_step_defaults_to_none(tmp_path):
    state_dir = tmp_path / "state"
    st = state.State(current_task="t1")
    st.save(state_dir)
    reloaded = state.State.load(state_dir)
    assert reloaded.current_step is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_state.py -k current_step -v`
Expected: FAIL with `TypeError: State.__init__() got an unexpected keyword argument 'current_step'`.

- [ ] **Step 3: Add the field**

In `harn/state.py`, the `State` dataclass (currently lines 22-29):

```python
@dataclass
class State:
    phase: str = PLANNING
    current_task: str | None = None
    question: str | None = None      # set when phase == BLOCKED
    blocked_since: float | None = None
    last_answer: str | None = None
    iterations: int = 0
```

Add `current_step` right after `current_task`:

```python
@dataclass
class State:
    phase: str = PLANNING
    current_task: str | None = None
    current_step: str | None = None  # set right before a SEQUENTIAL step's turn;
                                      # never set for parallel-wave members (see
                                      # Phase 4 spec's enforcement Non-goal)
    question: str | None = None      # set when phase == BLOCKED
    blocked_since: float | None = None
    last_answer: str | None = None
    iterations: int = 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_state.py -k current_step -v`
Expected: PASS.

- [ ] **Step 5: Wire `current_step` into `run()`'s sequential step-walk**

In `harn/loop.py`, find the sequential (non-wave, non-command) step turn inside `run()` — the block starting around line 1769 (`started = tasks._now_iso()` right before `_checkpoint_stage(project_root, task, sid)` and the `_run_turn(...)` call at line 1775). Add `current_step` bracketing right before that `_run_turn` call and clear it right after:

```python
            started = tasks._now_iso()
            if not auto:
                task.step_results[sid] = {"status": "running",
                                          "started": started, "ended": None}
                tasks._save(task)
            _checkpoint_stage(project_root, task, sid)
            st.current_step = sid
            st.save(state_dir)
            result = _run_turn(
                step_adapter, env_dir,
                _build_step_prompt(env_dir, cfg, task, step, feedback_tail, auto=auto),
                project_root, task_id=task.id, stage=sid, step_title=title,
                overrides=_step_overrides(cfg, step),
                tok_totals=tok_totals, tok_costs=tok_costs, cfg=cfg)
            st.current_step = None
            st.save(state_dir)
```

(Confirm `st`/`state_dir` are already in scope at this point in `run()` — they are, per the existing `_handle_block(env_dir, cfg, st, state_dir, task, auto=auto)` call a few lines below at line 1788, which already references both names.)

Do NOT make this change inside `_run_parallel_wave` (starting at line 1462) — per this phase's Non-goal, wave members never set `current_step` (a single shared `State` file cannot represent N concurrently-active steps); Task 5 threads step identity to wave members a different way (an env var, not `current_step`).

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 482 (prior count) + 2 = 484 passed.

- [ ] **Step 7: Bump version and commit**

Edit `harn/__init__.py`: `0.17.12` → `0.17.13`. Edit `pyproject.toml` to match.

```bash
git add harn/state.py harn/loop.py harn/__init__.py pyproject.toml tests/test_state.py
git commit -m "feat(state): current_step field, set around sequential step turns only; version 0.17.13"
```

---

### Task 5: `mcp_server.py` + `loop.py` — `tool_used` event on every MCP tool call

**Files:**
- Modify: `harn/mcp_server.py:127-140` (`build_server`'s `mcp = FastMCP("harn")` setup, right before the first `@mcp.tool()`)
- Modify: `harn/loop.py:1249-1276` (`_replicate_connectors` — add a `step_id` param that injects `HARN_STEP_ID` into each wave worktree's connector env)
- Modify: `harn/loop.py:1462+` (`_run_parallel_wave` — pass the step id to `_replicate_connectors`)
- Modify: `harn/events.py`'s docstring event vocabulary (add `tool_used`/`context_read`/`config_error` — these three exist or are being added but aren't documented in the vocabulary comment at the top of the file; a Phase 2 final review already flagged `context_read`/`config_error` as undocumented, so fix all three here while touching this area)
- Test: `tests/test_watch_autostart.py`-adjacent or a new `tests/test_tool_used_events.py`

**Interfaces:**
- Consumes: `harn/state.py`'s `State.current_step` (Task 4); `harn/loop.py`'s `_replicate_connectors(project_root, worktree_path, env_dir)` (Phase 3, being extended here with an optional 4th param).
- Produces: a `tool_used` event (`task_id`, `step_id`, `tool=<name>`) emitted on EVERY MCP tool call (not just skill/service/PRD reads, which already emit `context_read` via `_context_read`). Task 6 (enforcement) reads BOTH `tool_used` and `context_read` events scoped to a step. `_replicate_connectors(project_root, worktree_path, env_dir, step_id="")` — the new 4th param, default `""` for backward compat with Task 4/Phase-3 call sites that don't pass it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tool_used_events.py`:

```python
"""Every MCP tool call emits a tool_used event scoped to the currently
claimed task and (for a sequential step) the currently running step."""
import os
from pathlib import Path

from harn import events, mcp_server, state, tasks


def _env(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")  # never auto-start watch
    return env


def test_every_tool_call_emits_tool_used_tagged_to_current_task_and_step(
        tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id, current_step="s1")
    st.save(state_dir)

    server = mcp_server.build_server(start_watch=False)
    # FastMCP registers tools by name; call list_skills() through the
    # server's tool registry rather than importing the inner function
    # directly, so this test exercises the REAL wrapping path.
    tool = server._tool_manager._tools["list_skills"]  # adjust to FastMCP's
    # actual registry attribute name — confirm via a quick REPL/grep of the
    # installed `mcp` package's FastMCP class before writing this line for
    # real, since this is the one part of this test that depends on a
    # third-party library's internals rather than harn's own code.
    tool.fn()

    evs = [e for e in events.read(env, task_id=task.id) if e["event"] == "tool_used"]
    assert len(evs) == 1
    assert evs[0]["tool"] == "list_skills"
    assert evs[0]["step_id"] == "s1"


def test_tool_call_with_no_claimed_task_emits_nothing(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    server = mcp_server.build_server(start_watch=False)
    tool = server._tool_manager._tools["list_skills"]
    tool.fn()
    assert events.read(env) == []
```

(The FastMCP internal-registry lookup in the first test is inherently a bit fragile — before finalizing this test, run a quick throwaway script importing `mcp.server.fastmcp.FastMCP` and inspecting a built server's attributes to find the real way to invoke a registered tool by name, and use that. This is the one place in this task where you must verify against the actual installed `mcp` package rather than trust this plan's guess.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tool_used_events.py -v`
Expected: FAIL — no `tool_used` events exist yet.

- [ ] **Step 3: Add the central tool-call wrapper in `build_server`**

In `harn/mcp_server.py`, find `build_server` (currently starting at line 127):

```python
def build_server(start_watch: bool = True):
    from mcp.server.fastmcp import FastMCP, Image  # lazy: core CLI has no hard dep

    mcp = FastMCP("harn")

    if start_watch:
        ...
```

Right after `mcp = FastMCP("harn")` and before the `if start_watch:` block, add:

```python
    def _record_tool_used(tool_name: str) -> None:
        """Tag every MCP tool call with the currently claimed task (and,
        for a sequential step, the currently running step) so studio can
        later show which skills/tools a step actually used. Wraps
        `mcp.tool()` ONCE here instead of touching each of the 35+
        individual tool functions below."""
        try:
            env = _env_dir()
            st = state_mod.State.load(env / "state")
            if not st.current_task:
                return
            step_id = os.environ.get("HARN_STEP_ID", "") or st.current_step or ""
            events_mod.emit(env, "tool_used", task_id=st.current_task,
                            step_id=step_id or None, tool=tool_name)
        except Exception:
            pass

    _orig_tool = mcp.tool

    def _tracked_tool(*deco_args, **deco_kwargs):
        inner_decorator = _orig_tool(*deco_args, **deco_kwargs)

        def wrap(fn):
            import functools

            @functools.wraps(fn)
            def traced(*args, **kwargs):
                _record_tool_used(fn.__name__)
                return fn(*args, **kwargs)
            return inner_decorator(traced)
        return wrap

    mcp.tool = _tracked_tool
```

This wraps every subsequent `@mcp.tool()` call in the rest of `build_server` (all 35 of them, lines 141 onward) without editing a single one of them.

- [ ] **Step 4: Extend `_replicate_connectors` to inject `HARN_STEP_ID`**

In `harn/loop.py`, find `_replicate_connectors` (currently lines 1249-1276):

```python
def _replicate_connectors(project_root: Path, worktree_path: Path,
                          env_dir: Path) -> None:
    """..."""
    for rel in (".mcp.json", ".cursor/mcp.json"):
        src = project_root / rel
        if not src.exists():
            continue
        dst = worktree_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            cfg_json = json.loads(src.read_text(encoding="utf-8"))
            harn_server = cfg_json.get("mcpServers", {}).get("harn")
            if harn_server is not None:
                harn_server.setdefault("env", {})["HARN_ENV_DIR"] = str(env_dir.resolve())
            dst.write_text(json.dumps(cfg_json, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            shutil.copy(src, dst)  # best-effort: copy verbatim if we can't parse it
    for rel in ("AGENTS.md", "CLAUDE.md"):
        src = project_root / rel
        if src.exists():
            shutil.copy(src, worktree_path / rel)
```

Add a `step_id: str = ""` parameter and set `HARN_STEP_ID` in the copied connector's `env` block alongside the existing `HARN_ENV_DIR` rewrite:

```python
def _replicate_connectors(project_root: Path, worktree_path: Path,
                          env_dir: Path, step_id: str = "") -> None:
    """... (docstring unchanged) ...

    `step_id`, when given, is also written into the copied connector's
    `env.HARN_STEP_ID` — this is how a parallel-wave member's MCP server
    subprocess (sharing the ONE main harn_env, per the absolute
    HARN_ENV_DIR rewrite above) can still tag its `tool_used` events with
    the correct step id, even though `state.State.current_step` cannot
    represent more than one concurrently-active step (see Phase 4 spec's
    enforcement Non-goal)."""
    for rel in (".mcp.json", ".cursor/mcp.json"):
        src = project_root / rel
        if not src.exists():
            continue
        dst = worktree_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            cfg_json = json.loads(src.read_text(encoding="utf-8"))
            harn_server = cfg_json.get("mcpServers", {}).get("harn")
            if harn_server is not None:
                env_block = harn_server.setdefault("env", {})
                env_block["HARN_ENV_DIR"] = str(env_dir.resolve())
                if step_id:
                    env_block["HARN_STEP_ID"] = step_id
            dst.write_text(json.dumps(cfg_json, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            shutil.copy(src, dst)  # best-effort: copy verbatim if we can't parse it
    for rel in ("AGENTS.md", "CLAUDE.md"):
        src = project_root / rel
        if src.exists():
            shutil.copy(src, worktree_path / rel)
```

- [ ] **Step 5: Pass the step id at the one call site**

In `harn/loop.py`'s `_run_parallel_wave` (around line 1506), find:

```python
                _replicate_connectors(project_root, wt, env_dir)
```

Replace with:

```python
                _replicate_connectors(project_root, wt, env_dir, step_id=sid)
```

- [ ] **Step 6: Document the event vocabulary**

In `harn/events.py`, the module docstring's "Event vocabulary" list (currently lines 13-21) is missing `context_read`, `config_error`, and the new `tool_used`. Add all three:

```
  context_read — a skill/service/PRD/guidance body was pulled into a turn's
                 context (kind=skill|service|prd|guidance, name=…)
  tool_used    — any MCP tool was invoked during a task's claimed turn
                 (tool=…, step_id=… when known)
  config_error — a workflow declaration harn can't safely honor was ignored
                 (detail=…)
```

(Insert this right after the existing `run_end` line in that docstring list.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tool_used_events.py -v`
Expected: PASS.

- [ ] **Step 8: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 484 (prior count) + 2 = 486 passed. Pay particular attention to `tests/test_parallel_waves.py` — the `_replicate_connectors` signature change must not break any existing call in that test file's fakes/mocks.

- [ ] **Step 9: Bump version and commit**

Edit `harn/__init__.py`: `0.17.13` → `0.17.14`. Edit `pyproject.toml` to match.

```bash
git add harn/mcp_server.py harn/loop.py harn/events.py harn/__init__.py pyproject.toml tests/test_tool_used_events.py
git commit -m "feat(mcp): tool_used event on every MCP tool call, threaded to wave members via HARN_STEP_ID; version 0.17.14"
```

---

### Task 6: `loop.py` — post-step usage audit + retry-once-then-BLOCKED enforcement

**Files:**
- Modify: `harn/loop.py` (new function `_audit_step_usage`; wire it into `run()`'s sequential step-walk after a step reports "ok", around line 1823-1831)
- Test: `tests/test_step_enforcement.py` (new file)

**Interfaces:**
- Consumes: `workflow.py`'s `required`/`skills_recommended`/`tools`/`tools_recommended` fields (Task 1); `events.read(env_dir, task_id=...)` filtered for `tool_used`/`context_read` events whose `step_id` matches the step (Task 4/5).
- Produces: `_audit_step_usage(env_dir, task, step) -> dict` returning `{"skills": {name: "used"|"unused_recommended"|"unused_required"}, "tools": {...}}` — Task 7 (studio badges) reads this SAME shape (persisted onto `task.step_results[sid]["usage"]`, see below) to render 🟢/🟡/🔴.

This is the core enforcement task. Per the spec and this plan's Global Constraints, enforcement (retry-once, then a harness-authored BLOCKED) applies ONLY to sequential steps — a step inside a parallel wave still gets its usage AUDITED (so the data/badges are correct) but never retried/blocked automatically.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_step_enforcement.py`:

```python
"""Post-step required/recommended skill+tool usage audit and retry-once
enforcement (Phase 4), sequential steps only."""
from harn import events, loop, state, tasks, workflow
from harn.config import Config


def _env(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    return env, project_root


def test_audit_marks_used_recommended_and_unused_required(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    step = {"id": "s1", "required": ["standards"], "skills_recommended": ["ui"],
            "tools": ["run_tests"], "tools_recommended": ["read_design"]}
    events.emit(env, "context_read", task_id=task.id, step_id="s1",
                kind="skill", name="standards")
    events.emit(env, "tool_used", task_id=task.id, step_id="s1", tool="run_tests")
    usage = loop._audit_step_usage(env, task, step)
    assert usage["skills"]["standards"] == "used"
    assert usage["skills"]["ui"] == "unused_recommended"
    assert usage["tools"]["run_tests"] == "used"
    assert usage["tools"]["read_design"] == "unused_recommended"


def test_audit_flags_unused_required_skill(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    step = {"id": "s1", "required": ["standards"], "skills_recommended": [],
            "tools": [], "tools_recommended": []}
    usage = loop._audit_step_usage(env, task, step)
    assert usage["skills"]["standards"] == "unused_required"


def test_required_and_unused_triggers_exactly_one_retry_then_blocked(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    workflow.snapshot_for_task(env, task.id, [
        {"title": "Only step", "kind": "step", "id": "s1",
         "required": ["standards"], "skills_recommended": [],
         "tools": [], "tools_recommended": [], "enabled": True},
    ])
    cfg = Config()

    class NeverReadsSkillAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd=None, **kw):
            class R:
                ok = True
                text = "done, but never called read_skill"
                total_tokens = 5
                def tail(self, n): return "done"
                def usage_str(self): return ""
            return R()

    loop.run(project_root, env, max_iterations=5,
             adapter=NeverReadsSkillAdapter(), cfg=cfg)

    fresh = tasks.find(env, task.id)
    state_dir = env / "state"
    # Exactly one retry happened (the retry prompt reminder text was used once),
    # then the harness itself wrote BLOCKED — never a second silent retry.
    assert fresh.step_results["s1"]["status"] in ("blocked",)
    st = state.State.load(state_dir)
    assert st.phase == state.BLOCKED
    assert "standards" in (st.question or "")


def test_recommended_and_unused_never_retries(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    workflow.snapshot_for_task(env, task.id, [
        {"title": "Only step", "kind": "step", "id": "s1",
         "required": [], "skills_recommended": ["ui"],
         "tools": [], "tools_recommended": [], "enabled": True},
    ])
    cfg = Config()

    class PlainAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd=None, **kw):
            class R:
                ok = True
                text = "done"
                total_tokens = 5
                def tail(self, n): return "done"
                def usage_str(self): return ""
            return R()

    loop.run(project_root, env, max_iterations=2, adapter=PlainAdapter(), cfg=cfg)
    fresh = tasks.find(env, task.id)
    assert fresh.step_results["s1"]["status"] == "ok"
```

(Adjust the fake adapter's exact interface — `run_turn`'s parameter names/return shape — to match whatever `_pick_adapter`/adapter protocol `harn/loop.py`'s real adapters implement; grep `harn/adapters/` for the base class/protocol before writing these fakes for real, rather than guessing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_step_enforcement.py -v`
Expected: FAIL — `AttributeError: module 'harn.loop' has no attribute '_audit_step_usage'`.

- [ ] **Step 3: Implement `_audit_step_usage`**

In `harn/loop.py`, add this function near `_build_step_prompt` (e.g. right after `preview_step_prompt`/`save_context_export` from Task 3):

```python
def _audit_step_usage(env_dir: Path, task: "tasks.Task", step: dict) -> dict:
    """Compare a step's declared required/recommended skills+tools against
    what actually got used during its window (tool_used + context_read
    events scoped to this step's id). Returns
    {"skills": {name: "used"|"unused_recommended"|"unused_required"},
     "tools":  {name: "used"|"unused_recommended"|"unused_required"}}."""
    sid = step.get("id") or ""
    evs = events.read(env_dir, task_id=task.id)
    used_skill_names = {e.get("name") for e in evs
                        if e.get("event") == "context_read"
                        and e.get("kind") == "skill" and e.get("step_id") == sid}
    used_tool_names = {e.get("tool") for e in evs
                       if e.get("event") == "tool_used" and e.get("step_id") == sid}

    def _tier(name: str, required: list, recommended: list, used: set) -> str:
        if name in used:
            return "used"
        if name in required:
            return "unused_required"
        return "unused_recommended"

    req_skills = step.get("required") or []
    rec_skills = step.get("skills_recommended") or []
    req_tools = step.get("tools") or []
    rec_tools = step.get("tools_recommended") or []
    skills_out = {n: _tier(n, req_skills, rec_skills, used_skill_names)
                 for n in [*req_skills, *rec_skills]}
    tools_out = {n: _tier(n, req_tools, rec_tools, used_tool_names)
                for n in [*req_tools, *rec_tools]}
    return {"skills": skills_out, "tools": tools_out}


_REQUIRED_UNUSED_RETRY_NOTE = (
    "## You skipped a required skill or tool last time\n"
    "Your previous attempt at this step did NOT use the following "
    "REQUIRED skill(s)/tool(s): {names}. You MUST use it/them this time "
    "before finishing this step."
)
```

- [ ] **Step 4: Wire enforcement into `run()`'s sequential step-walk**

In `harn/loop.py`'s `run()`, find the "Step done" block (currently around lines 1823-1835):

```python
            # Step done — ledger it and advance to the next step next iteration.
            feedback_tail = ""
            if auto:
                done_ids.add(sid)
            else:
                task.step_results[sid] = {"status": "ok", "started": started,
                                          "ended": tasks._now_iso(),
                                          "tokens": result.total_tokens,
                                          "output": (result.text or "")[-4000:]}
                tasks._save(task)
                task = tasks.find(env_dir, task.id) or task
            # More steps remain? loop to run the next one.
            if any(not _step_done(s.get("id")) for s in steps):
                continue
```

Replace with (auto mode is unaffected — enforcement only applies to non-auto, ledgered runs, matching the spec's framing of this as a human-visible studio feature):

```python
            # Step done — audit required/recommended usage before ledgering.
            feedback_tail = ""
            if auto:
                done_ids.add(sid)
            else:
                usage = _audit_step_usage(env_dir, task, step)
                unused_required = [n for n, v in {**usage["skills"], **usage["tools"]}.items()
                                   if v == "unused_required"]
                retried_key = f"_retried_{sid}"
                if unused_required and not getattr(task, "_enforcement_retries", {}).get(sid):
                    task_retries = getattr(task, "_enforcement_retries", None)
                    if task_retries is None:
                        task._enforcement_retries = {}
                    task._enforcement_retries[sid] = True
                    feedback_tail = _REQUIRED_UNUSED_RETRY_NOTE.format(
                        names=", ".join(unused_required))
                    task.step_results[sid] = {"status": "running",
                                              "started": started, "ended": None,
                                              "usage": usage}
                    tasks._save(task)
                    continue  # same step, one retry, with the reminder as feedback
                if unused_required:
                    # Already retried once and still unused — harness blocks.
                    detail = (f"Required skill/tool still unused after one retry: "
                             f"{', '.join(unused_required)}")
                    state.blocked_marker(state_dir).write_text(detail, encoding="utf-8")
                    st.block(detail)
                    st.save(state_dir)
                    task.step_results[sid] = {"status": "blocked", "started": started,
                                              "ended": tasks._now_iso(), "usage": usage}
                    tasks._save(task)
                    return _run_end(env_dir, st)
                task.step_results[sid] = {"status": "ok", "started": started,
                                          "ended": tasks._now_iso(),
                                          "tokens": result.total_tokens,
                                          "output": (result.text or "")[-4000:],
                                          "usage": usage}
                tasks._save(task)
                task = tasks.find(env_dir, task.id) or task
            # More steps remain? loop to run the next one.
            if any(not _step_done(s.get("id")) for s in steps):
                continue
```

Note: `task._enforcement_retries` is an in-memory, per-run-process dict (not persisted to the task's JSON — it only needs to survive across the SAME `run()` call's loop iterations, exactly like the existing `tests_nudged`/`done_ids` in-memory sets a few lines earlier in the same function). Follow that SAME existing pattern rather than adding a new persisted field: find where `tests_nudged = set()` (or similar) is initialized near the top of `run()` and add `enforcement_retried: set[tuple[str, str]] = set()` alongside it (keyed by `(task.id, sid)` so it's safe across multiple tasks in one `run()` invocation), then replace the `task._enforcement_retries` logic above with checks against that set instead — this matches the codebase's established idiom for "an in-memory per-run debounce" (see the Phase 2 ledger's own note about the `reworked` debounce set) rather than inventing a new pattern of mutating the task object with an undeclared attribute.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_step_enforcement.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 486 (prior count) + 4 = 490 passed.

- [ ] **Step 7: Confirm parallel-wave steps are unaffected (Non-goal check)**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_parallel_waves.py -q`
Expected: PASS, unchanged count — this task must not have touched `_run_parallel_wave`/`_merge_wave_patches` at all. If any parallel-wave test fails, you've accidentally wired enforcement into the wave path; revert that part.

- [ ] **Step 8: Bump version and commit**

Edit `harn/__init__.py`: `0.17.14` → `0.17.15`. Edit `pyproject.toml` to match.

```bash
git add harn/loop.py harn/__init__.py pyproject.toml tests/test_step_enforcement.py
git commit -m "feat(loop): post-step required/recommended usage audit + retry-once-then-BLOCKED enforcement (sequential steps only); version 0.17.15"
```

---

### Task 7: `studio.py` — skill/tool usage badges (🟢/🟡/🔴) in the step inspector

**Files:**
- Modify: `harn/studio.py` (the `task_plan_payload` function or equivalent that already serializes a task's steps to JSON for the frontend — grep to find it; ensure `step_results[sid]["usage"]` from Task 6 reaches the JSON payload) and the inspector-rendering JS (wherever the existing skill/tool "toggle chips" are rendered — grep for how `required`/`tools` are currently displayed per step)
- Test: manual verification via the dev server (this is UI-only; add a Python-level test only for the payload plumbing, not the rendering)

**Interfaces:**
- Consumes: `task.step_results[sid]["usage"]` (Task 6's exact shape: `{"skills": {name: tier}, "tools": {name: tier}}`).
- Produces: no new function signatures — purely a rendering change plus ensuring the existing task-plan JSON payload includes the `usage` field already present in the ledger.

- [ ] **Step 1: Write the failing test for payload plumbing**

Find the existing test file covering `task_plan_payload` (grep `task_plan_payload` across `tests/`) and add:

```python
def test_task_plan_payload_includes_step_usage_when_present(tmp_path):
    env, project_root = _env(tmp_path)  # reuse this file's existing helper
    task = tasks.create_task(env, "Do the thing")
    task.step_results["s1"] = {"status": "ok", "usage": {
        "skills": {"standards": "used"}, "tools": {}}}
    tasks._save(task)
    payload = studio.task_plan_payload(env, task.id)
    step = next(s for s in payload["nodes"] if s.get("id") == "s1"
               or True)  # adjust the lookup to however this payload keys steps
    assert payload["step_results"]["s1"]["usage"]["skills"]["standards"] == "used"
```

(This test's exact shape depends on `task_plan_payload`'s real existing JSON structure — read that function first and adjust the assertion to match how it ALREADY nests `step_results` today, since Task 6 only added a new `usage` key inside an existing dict this payload function should already be passing through untouched.)

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_studio_api.py -k step_usage -v` (adjust filename to whatever the real test file is called)

If `task_plan_payload` already passes `step_results` through verbatim (likely, since it's just a dict already on the task object), this test may PASS immediately with zero code changes — in which case skip Step 3 and note in your report that the payload plumbing needed no change, only the frontend rendering (Step 4 below) is new work.

- [ ] **Step 3: If needed, ensure `usage` passes through**

Only if Step 2's test failed: find wherever `task_plan_payload` builds each step's JSON dict and confirm it spreads/copies the FULL `step_results[sid]` dict (not a hand-picked subset of keys) — if it hand-picks keys, add `"usage": entry.get("usage", {})` to that dict literal.

- [ ] **Step 4: Render the badges in the step inspector**

Find where the step inspector currently renders a step's `required`/`tools` chips (grep `renderInsp` or wherever `n.required`/`n.tools` are turned into HTML chip elements in `harn/studio.py`'s `<script>` block — Phase 3 Task 7's report referenced this same neighborhood for the parallel-group note). For each skill/tool chip, add a border-color class based on the step's `usage` data (read from `BOARD`/`selTaskStepResult(n.id)` — whichever existing helper already fetches a step's `step_results` entry client-side; Phase 2's report mentions `selTaskStepResult(stepId)` as an existing helper in `harn/studio.py`, reuse it):

```javascript
function usageBadgeClass(stepId, kind, name){
  const res = selTaskStepResult(stepId);
  const usage = res && res.usage && res.usage[kind];
  const tier = usage && usage[name];
  if(tier === 'used') return 'badge-used';
  if(tier === 'unused_required') return 'badge-unused-required';
  if(tier === 'unused_recommended') return 'badge-unused-recommended';
  return '';
}
```

Add matching CSS near the existing chip styles (grep for `.chip` or `.skillrow` class definitions in the `<style>` block):

```css
.badge-used{ border-color: #2ecc71; }
.badge-unused-recommended{ border-color: #f1c40f; }
.badge-unused-required{ border-color: #e74c3c; animation: st-active 1.2s infinite; }
```

(`st-active` is the existing blink animation the codebase already uses elsewhere for "live/attention-needed" indicators — grep for `@keyframes st-active` or the `st-active` class to confirm its exact name and reuse it verbatim rather than defining a new animation.)

Apply `usageBadgeClass(...)` as an additional class on each required/recommended skill and tool chip's existing HTML template string.

- [ ] **Step 5: Verify JS syntax**

Run: `node --check /tmp/studio_check.js` (regenerate the extracted script first, per Task 3 Step 7's extraction command).
Expected: no output (syntax OK).

- [ ] **Step 6: Manual live verification**

Start the studio dev server against a scratch project with a task whose plan has a step declaring `Skills (required: standards; recommended: testing)`, run it with a fake/real adapter that reads `standards` but not `testing`, and confirm in a browser: the `standards` chip shows green, `testing` shows yellow. Then a step with an unused REQUIRED skill should show red and pulse. Record what you observed in your task report.

- [ ] **Step 7: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 490 (prior count), +0 or +1 depending on whether Step 3 was needed.

- [ ] **Step 8: Bump version and commit**

Edit `harn/__init__.py`: `0.17.15` → `0.17.16`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml
git commit -m "feat(studio): green/yellow/red usage badges for required/recommended skills+tools; version 0.17.16"
```

---

### Task 8: `studio.py` — Pause/Resume UI relabel (no engine change)

**Files:**
- Modify: `harn/studio.py` (the "■ Stop" button at the run-banner HTML, currently at line ~1170: `` `<button class="ghost" onclick="stopRun()">■ Stop</button>` ``)

**Interfaces:**
- Consumes: nothing new — `stopRun()`'s existing JS function and whatever backend route it already calls (`/api/tasks/stop`) are UNCHANGED.
- Produces: nothing new — this is a copy/labeling-only change.

- [ ] **Step 1: Manually confirm the existing resume path already works**

Before touching any code: start the studio dev server, launch a multi-step task, click Stop partway through, edit a not-yet-started step's body in the task plan editor, then click Relaunch/Run again. Confirm (per the spec's own framing — this is a verification step, not new functionality): the already-completed steps are skipped (`step_results[...]["status"] == "ok"` in `_step_done`, per `harn/loop.py`'s existing `_step_done` helper) and only the edited/remaining steps run. Record the observation in your task report — if this does NOT already work as described, STOP and report back rather than proceeding (this would mean the spec's premise, and hence this task's scope, is wrong).

- [ ] **Step 2: Update the button label and add the clarifying subtitle**

In `harn/studio.py`, find (currently around line 1170):

```javascript
      `<button class="ghost" onclick="stopRun()">■ Stop</button></div>`;
```

Replace with:

```javascript
      `<button class="ghost" onclick="stopRun()" title="Steps already done stay done; edit the plan, then ▶ Resume">⏸ Pause</button></div>`;
```

- [ ] **Step 3: Verify JS syntax**

Run: `node --check /tmp/studio_check.js` (regenerate the extracted script first).
Expected: no output.

- [ ] **Step 4: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — unchanged count (this task adds no new tests, only a label change plus the manual verification recorded in Step 1).

- [ ] **Step 5: Bump version and commit**

Edit `harn/__init__.py`: `0.17.16` → `0.17.17`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml
git commit -m "docs(studio): relabel Stop as Pause to make the existing resume path explicit; version 0.17.17"
```

---

### Task 9: `studio.py` — answer a BLOCKED question from studio

**Files:**
- Modify: `harn/studio.py` (new `GET /api/tasks/blocked_question` and `POST /api/tasks/answer` routes; extend `pollBoard()`'s rendering to show a question banner + answer box)
- Test: `tests/test_studio_api.py` (or wherever studio's payload functions are already tested — grep for existing `*_payload` tests)

**Interfaces:**
- Consumes: `harn/state.py`'s `State.load`/`State.question`/`State.phase` (unchanged); `harn/loop.py`'s existing `answer(env_dir, text)` function (the SAME one `harn/cli.py`'s `cmd_answer` already calls — grep `harn/loop.py` for `def answer(` to confirm its exact signature before wiring this).
- Produces: `blocked_question_payload(env_dir: Path, task_id: str) -> dict` returning `{"question": str}` or `{"question": None}`; a POST route that calls the existing `loop.answer(env_dir, text)`.

- [ ] **Step 1: Write the failing tests**

Add to the studio payload test file (grep `harn/studio.py`'s existing test coverage to find the right file — likely `tests/test_studio_api.py` or similar; check its existing imports/fixtures first):

```python
def test_blocked_question_payload_returns_question_when_blocked(tmp_path):
    env, project_root = _env(tmp_path)  # reuse this file's existing helper
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id)
    st.block("Should I use approach A or B? I recommend A because...")
    st.save(state_dir)
    payload = studio.blocked_question_payload(env, task.id)
    assert payload["question"] == "Should I use approach A or B? I recommend A because..."


def test_blocked_question_payload_returns_none_when_not_blocked(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    payload = studio.blocked_question_payload(env, task.id)
    assert payload["question"] is None


def test_answer_route_calls_loop_answer_and_clears_the_block(tmp_path):
    env, project_root = _env(tmp_path)
    task = tasks.create_task(env, "Do the thing")
    state_dir = env / "state"
    st = state.State(current_task=task.id)
    st.block("Pick one.")
    st.save(state_dir)
    result = studio.answer_payload(env, task.id, "Go with A.")
    assert result.get("ok") is True
    reloaded = state.State.load(state_dir)
    assert reloaded.phase != state.BLOCKED
    assert reloaded.last_answer == "Go with A."
```

(Confirm `loop.answer`'s exact call signature — grep `def answer(` in `harn/loop.py` — before writing `answer_payload`'s implementation in Step 3; this plan assumes `answer(env_dir: Path, text: str) -> None` based on `harn/cli.py:297`'s `cmd_answer` calling `loop.answer(env_dir, args.text)`, but verify against the real current signature since it may take additional params by now.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_studio_api.py -k blocked_question -v`
Expected: FAIL — `AttributeError: module 'harn.studio' has no attribute 'blocked_question_payload'`.

- [ ] **Step 3: Implement the two payload functions**

In `harn/studio.py`, near the other `*_payload` functions (e.g. next to `board_payload`), add:

```python
def blocked_question_payload(env_dir: Path, task_id: str) -> dict:
    """The pending BLOCKED question for this task's env, if any. The
    question text is the agent's own free-text prose (context, options,
    and its recommendation all embedded as written) — studio renders it
    verbatim in a <pre> block rather than parsing it into buttons, since
    there is no separate structured options list anywhere in harn."""
    st = state_mod.State.load(env_dir / "state")
    if st.phase == state_mod.BLOCKED and st.question:
        return {"question": st.question}
    return {"question": None}


def answer_payload(env_dir: Path, task_id: str, text: str) -> dict:
    if not text.strip():
        return {"error": "answer text is empty"}
    loop_mod.answer(env_dir, text)
    return {"ok": True}
```

(Use whichever real import alias `harn/studio.py` already has for `harn.loop` and `harn.state` — confirmed in Task 3's Step 5 note; do not introduce a new alias.)

- [ ] **Step 4: Wire the two routes**

In `harn/studio.py`'s `do_GET`, add (near the `/api/board` branch):

```python
            elif route == "/api/tasks/blocked_question":
                self._json(blocked_question_payload(env, self._query("task") or ""))
```

In `do_POST`, add (near the `/api/tasks/stop` branch):

```python
            elif route == "/api/tasks/answer":
                body = self._read_json()
                self._json(answer_payload(env, body.get("task", ""), body.get("text", "")))
```

- [ ] **Step 5: Add the banner + answer box to the Board tab**

In `harn/studio.py`'s `pollBoard()` (currently around line 1154-1160):

```javascript
async function pollBoard(){
  try{ BOARD=await (await fetch(api('/api/board'))).json(); }catch(e){ return; }
  if(tab!=='board') return;
  renderBoard();
  if(boardSel&&(BOARD.tasks||[]).some(t=>t.id===boardSel)) renderTaskDetail();
  else{ boardSel=null; $('#insp').innerHTML='<div class="empty">Select a task.</div>'; }
}
```

Add a call to fetch and render the blocked-question banner when a task is selected:

```javascript
async function pollBoard(){
  try{ BOARD=await (await fetch(api('/api/board'))).json(); }catch(e){ return; }
  if(tab!=='board') return;
  renderBoard();
  if(boardSel&&(BOARD.tasks||[]).some(t=>t.id===boardSel)){
    renderTaskDetail();
    await pollBlockedQuestion();
  } else{ boardSel=null; $('#insp').innerHTML='<div class="empty">Select a task.</div>'; }
}

async function pollBlockedQuestion(){
  if(!boardSel) return;
  const r = await (await fetch(api(`/api/tasks/blocked_question?task=${encodeURIComponent(boardSel)}`))).json();
  const el = $('#blockedBanner');
  if(!el) return;
  if(r.question){
    el.style.display='block';
    el.innerHTML = `<div class="blockedq"><b>Blocked — needs your answer:</b>`+
      `<pre>${esc(r.question)}</pre>`+
      `<textarea id="answerBox" rows="3" placeholder="Your answer..."></textarea>`+
      `<button onclick="submitAnswer()">Submit answer</button></div>`;
  } else {
    el.style.display='none'; el.innerHTML='';
  }
}

async function submitAnswer(){
  const text = $('#answerBox').value;
  if(!text.trim()){ alert('Enter an answer first.'); return; }
  const r = await post_('/api/tasks/answer', {task: boardSel, text});
  if(r.error){ alert(r.error); return; }
  $('#blockedBanner').style.display='none';
  $('#blockedBanner').innerHTML='';
  pollBoard();
}
```

Add the `#blockedBanner` container element to the Board tab's static HTML shell (find where `#insp` — the inspector panel div — is declared in the Board tab's layout HTML and add a sibling div right before it):

```html
<div id="blockedBanner" style="display:none;"></div>
```

Add matching CSS near the existing `.runbanner` style (grep for `.runbanner{` in the `<style>` block):

```css
.blockedq{ background:#3a2a10; border:1px solid #e74c3c; padding:10px; border-radius:6px; margin-bottom:8px; }
.blockedq pre{ white-space:pre-wrap; max-height:200px; overflow-y:auto; }
.blockedq textarea{ width:100%; margin-top:6px; }
```

- [ ] **Step 6: Verify JS syntax**

Run: `node --check /tmp/studio_check.js` (regenerate first).
Expected: no output.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_studio_api.py -k "blocked_question or answer_route" -v`
Expected: PASS.

- [ ] **Step 8: Manual live verification**

Start the studio dev server, run a task with a step whose prompt/fixture forces an `ask_user` call (or manually write a `BLOCKED.md`/`STATE.json` for a test task), confirm the Board tab shows the banner with the question text, submit an answer, confirm the banner disappears and the task resumes.

- [ ] **Step 9: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 490 (Task 6's count, assuming Task 7 added 0-1) + 3 = 493 or 494 passed.

- [ ] **Step 10: Bump version and commit**

Edit `harn/__init__.py`: `0.17.17` → `0.17.18`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml tests/test_studio_api.py
git commit -m "feat(studio): answer a BLOCKED question directly from studio, reusing loop.answer(); version 0.17.18"
```

---

### Task 10: Documentation

**Files:**
- Modify: `README.md` (both the English and Russian sections — following the same bilingual mirror convention Phase 3 Task 8 used for the `Parallel:` section)

**Interfaces:**
- Consumes: nothing — pure documentation.
- Produces: nothing new later tasks depend on (this is the final task).

- [ ] **Step 1: Read the current state of the README's per-step field documentation**

Read the `Parallel:`/`Type:`/`Command:`/`On fail:` section Phase 3 Task 8 added to `README.md` (grep `Parallel:` in `README.md` to find it) — this task's new section goes immediately after it, in both the English and Russian halves, following the identical structural pattern (heading level, code-block style, terse 1-2 sentence explanations).

- [ ] **Step 2: Write the English section**

Add a new subsection (matching the existing heading level) covering:
- The `Skills (required: a; recommended: b, c)` and `Tools (required: x; recommended: y)` syntax, noting the old bare forms (`Skills (required: a)`, `Tools: x, y`) still work exactly as before.
- After a step runs, studio's inspector shows green/yellow/red badges on each declared skill/tool: used, recommended-but-unused, required-but-unused.
- A required-and-unused skill/tool triggers exactly one automatic retry of that same step with a reminder; if still unused, harn itself blocks the run for a human to resolve — this does NOT apply to steps running inside a parallel wave (usage is still recorded and shown, but no automatic retry).
- The step inspector's "View full context" button shows the exact prompt a step will receive (or did receive), with a "Copy to file" export.
- The Board's "⏸ Pause" button (renamed from Stop) — steps already marked done stay done; edit the plan and click Resume to continue only the remaining steps.
- When a run is BLOCKED, the Board shows the question directly (with a text box to answer it) instead of requiring `harn answer` from a terminal — unchanged: if nobody answers within the configured grace period, harn still escalates the same question to Telegram.

- [ ] **Step 3: Mirror the section in Russian**

Add the same content under the `## harn — на русском` half, in the same relative position, following the file's existing bilingual-mirror convention exactly (verify by re-reading how Phase 3 Task 8's `Parallel:` section was mirrored).

- [ ] **Step 4: Run the full suite (sanity check — docs-only change)**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — unchanged count from Task 9.

- [ ] **Step 5: Bump version and commit**

Edit `harn/__init__.py`: `0.17.18` → `0.17.19`. Edit `pyproject.toml` to match.

```bash
git add README.md harn/__init__.py pyproject.toml
git commit -m "docs: document Phase 4 — skill/tool tiers, usage badges, context preview/export, Pause label, in-studio answer; version 0.17.19"
```
