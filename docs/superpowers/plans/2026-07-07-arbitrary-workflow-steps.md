# Arbitrary Workflow Steps (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `harn run` executes the task's OWN workflow plan — any number of user-defined steps, each as a separate agent call with its own agent/model/effort/temperature — replacing the hardcoded six-stage pipeline.

**Architecture:** Per-step fields (`Id:`/`Agent:`/`Model:`/`Effort:`/`Temperature:`) parsed from WORKFLOW.md into workflow nodes; each task snapshots its preset into `harn_env/tasks/<id>.workflow.json` at creation; a generic step engine in `loop.py` walks the snapshot's enabled steps, checkpointing each in git (key = step id) and recording a durable `task.step_results` ledger. The studio canvas edits both presets AND a task's own plan; every step's inspector offers agent/model controls unconditionally.

**Tech Stack:** Python stdlib only (existing constraint), pytest, single-file vanilla-JS studio.

**Spec:** `docs/superpowers/specs/2026-07-06-arbitrary-workflow-stages-design.md`

## Global Constraints

- Provider-agnostic: works identically for claude / codex / cursor / qwen / antigravity adapters; per-step `Agent:` may name any of them.
- Stdlib-only in `harn/` (no new dependencies).
- `harn.toml`'s `[models.<stage>]` tables and `MODEL_STAGES` are REMOVED (breaking change approved in spec). `[harn] agent` / `[harn] model` remain the global defaults.
- Step id format: `step-` + 6 lowercase hex chars (e.g. `step-3f8a9c`), generated via `uuid.uuid4().hex[:6]`, stable across renames.
- Checkpoint dict on Task keeps its serialized name `stage_checkpoints` (backward-compatible task JSON); its KEYS become step ids.
- `oracle_review()` and `reconcile_headless()` in `loop.py` are used by `watch()` (chat-mode coordinator) — they must keep working (their `_stage_overrides(cfg, "oracle"/"reconcile")` calls are replaced by plain defaults).
- Every commit ends with the trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Run the full suite with `python3 -m pytest -q` from the repo root; JS syntax check via extracting the `<script>` block from `harn/studio.py` and running `node --check` (pattern used throughout this repo's history — see Task 6 Step 6).

---

### Task 1: Per-step fields in workflow.py (Id/Agent/Model/Effort/Temperature)

**Files:**
- Modify: `harn/workflow.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `parse()` node dicts gain keys `id, agent, model, effort, temperature` (all `str`, default `""`); `compose()` writes the corresponding lines when set; new function `ensure_ids(parsed: dict) -> dict` assigns `step-<6hex>` ids to every step node lacking one (mutates and returns `parsed`). The old `stage` node key and `Stage:` line are GONE (legacy `Stage:` lines are consumed and discarded on parse so they don't leak into body text).

- [ ] **Step 1: Write failing tests**

Replace the whole `# --- explicit per-step Stage mapping ...` section of `tests/test_workflow.py` (the six tests `test_parse_defaults_stage_to_empty` through `test_renaming_step_does_not_move_the_stage_mapping`) with:

```python
# --- per-step execution fields (Id / Agent / Model / Effort / Temperature) - #

def test_parse_defaults_step_fields_to_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    for n in parsed["nodes"]:
        if n["kind"] == "step":
            assert n["id"] == "" and n["agent"] == "" and n["model"] == ""
            assert n["effort"] == "" and n["temperature"] == ""


def test_step_fields_round_trip_through_compose(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step.update(id="step-3f8a9c", agent="cursor", model="composer-1",
                effort="high", temperature="0.2")
    workflow.save_parsed(env, parsed)
    re = workflow.parse(env)
    s2 = next(n for n in re["nodes"] if n["title"] == "Implement")
    assert s2["id"] == "step-3f8a9c" and s2["agent"] == "cursor"
    assert s2["model"] == "composer-1" and s2["effort"] == "high"
    assert s2["temperature"] == "0.2"
    # other steps untouched
    other = next(n for n in re["nodes"] if n["title"] == "Session start — orient")
    assert other["agent"] == "" and other["id"] == ""


def test_id_survives_rename(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step["id"] = "step-aa11bb"
    step["title"] = "Build the thing"
    workflow.save_parsed(env, parsed)
    re = workflow.parse(env)
    assert next(n for n in re["nodes"]
                if n["title"] == "Build the thing")["id"] == "step-aa11bb"


def test_ensure_ids_fills_only_missing(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step["id"] = "step-keepme"
    out = workflow.ensure_ids(parsed)
    ids = [n["id"] for n in out["nodes"] if n["kind"] == "step"]
    assert all(ids), "every step got an id"
    assert "step-keepme" in ids
    assert len(set(ids)) == len(ids), "ids are unique"
    for i in ids:
        if i != "step-keepme":
            assert re_mod.fullmatch(r"step-[0-9a-f]{6}", i), i


def test_legacy_stage_line_is_dropped_silently(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    p = env / "WORKFLOW.md"
    p.write_text(p.read_text().replace(
        "## 3. Implement", "## 3. Implement\nStage: execute"), encoding="utf-8")
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    assert "Stage:" not in step["body"]
    assert "stage" not in step  # the key no longer exists
```

Add at the top of the test file: `import re as re_mod`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_workflow.py -q`
Expected: the five new tests FAIL (`KeyError: 'id'`, missing `ensure_ids`).

- [ ] **Step 3: Implement in workflow.py**

In `harn/workflow.py`:

(a) Delete `from .config import MODEL_STAGES` and the `_STAGE_RE` docs; replace the regex block with:

```python
# Per-step execution fields: which CLI runs this step under `harn run`, with
# which model/effort/temperature, and the stable id that survives renames
# (checkpoint + Run/Rerun key). Explicit lines, same convention as
# `Skills (required: …)` / `Tools:` — never guessed from the title.
_ID_RE = re.compile(r"^\s*Id:\s*(\S+)\s*$", re.IGNORECASE)
_AGENT_RE = re.compile(r"^\s*Agent:\s*(\S+)\s*$", re.IGNORECASE)
_MODEL_RE = re.compile(r"^\s*Model:\s*(\S+)\s*$", re.IGNORECASE)
_EFFORT_RE = re.compile(r"^\s*Effort:\s*(\S+)\s*$", re.IGNORECASE)
_TEMP_RE = re.compile(r"^\s*Temperature:\s*(\S+)\s*$", re.IGNORECASE)
# Legacy line from the removed fixed-stage system: recognised and DISCARDED.
_STAGE_RE = re.compile(r"^\s*Stage:\s*([a-z_]+)\s*$", re.IGNORECASE)
```

(b) In `parse()`: initialize each new step node with `"id": "", "agent": "", "model": "", "effort": "", "temperature": ""` (replacing `"stage": ""`), and replace the `Stage:` handling block with:

```python
if _STAGE_RE.match(line):
    cur["_decl"] = True      # legacy line: swallow, don't put in body
    continue
for rx, key in ((_ID_RE, "id"), (_AGENT_RE, "agent"), (_MODEL_RE, "model"),
                (_EFFORT_RE, "effort"), (_TEMP_RE, "temperature")):
    m2 = rx.match(line)
    if m2:
        cur["_decl"] = True
        cur[key] = m2.group(1).strip()
        break
else:
    ...existing body-line handling...
```

(Adapt to the file's actual loop structure: the existing code uses sequential `if` blocks with `continue` — follow that pattern, one block per regex, each setting `cur["_decl"]=True`, storing the value, `continue`.)

(c) In `compose()`: replace the `Stage:` writing block with:

```python
for key, label in (("id", "Id"), ("agent", "Agent"), ("model", "Model"),
                   ("effort", "Effort"), ("temperature", "Temperature")):
    val = str(n.get(key) or "").strip()
    if kind == "step" and val:
        out.append(f"{label}: {val}")
```

(d) Add `ensure_ids`:

```python
def ensure_ids(parsed: dict) -> dict:
    """Assign a stable `step-<6hex>` id to every step node lacking one.
    Ids are the durable key for git checkpoints, the step ledger, and
    Run/Rerun — they survive renaming the step. Mutates and returns parsed."""
    import uuid
    seen = {n.get("id") for n in parsed.get("nodes", []) if n.get("id")}
    for n in parsed.get("nodes", []):
        if n.get("kind") == "step" and not n.get("id"):
            nid = f"step-{uuid.uuid4().hex[:6]}"
            while nid in seen:
                nid = f"step-{uuid.uuid4().hex[:6]}"
            n["id"] = nid
            seen.add(nid)
    return parsed
```

(e) In `save_parsed()`: call `ensure_ids(parsed)` before composing, so any save from the studio stamps ids.

(f) Update `parse()`'s docstring: document the five fields and that `Stage:` is legacy-discarded.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_workflow.py -q`
Expected: all pass.

- [ ] **Step 5: Run the full suite; fix ONLY collateral compile errors**

Run: `python3 -m pytest -q 2>&1 | tail -15`
Expected: failures ONLY in tests referencing the removed `stage` key (`test_studio_models.py`, `test_run_stage.py`, `test_stage_checkpoints.py`, `test_stage_launch.py`, `test_stage_models.py`) and possibly `harn/studio.py` imports. Do NOT fix those tests now (Tasks 5–7 replace them). If `harn/studio.py` fails to IMPORT because `workflow.py` no longer exports something it used, patch studio minimally (e.g. keep its own `MODEL_STAGES` import from config — untouched until Task 7). Record the failing-test list in the commit message body.

- [ ] **Step 6: Commit**

```bash
git add harn/workflow.py tests/test_workflow.py
git commit -m "feat(workflow): per-step Id/Agent/Model/Effort/Temperature fields, drop Stage:"
```

---

### Task 2: Per-task workflow snapshot + step ledger

**Files:**
- Modify: `harn/workflows.py`, `harn/tasks.py`
- Test: `tests/test_task_plan.py` (create)

**Interfaces:**
- Consumes: `workflow.ensure_ids(parsed)` from Task 1; existing `workflows.load(env_dir, name)`, `workflows._ensure_default(env_dir)`, `workflows.render(env_dir, wf)`.
- Produces (in `harn/workflows.py`):
  - `task_plan_path(env_dir: Path, task_id: str) -> Path` → `env_dir / "tasks" / f"{task_id}.workflow.json"`
  - `snapshot_for_task(env_dir: Path, task_id: str, preset: str | None) -> dict` — deep-copies the named preset (or the default workflow), runs `ensure_ids`, writes the JSON file, returns the dict. Idempotent: if the file already exists, loads and returns it unchanged.
  - `load_task_plan(env_dir: Path, task_id: str) -> dict | None`
  - `save_task_plan(env_dir: Path, task_id: str, parsed: dict) -> Path` (runs `ensure_ids` first)
  - `activate_task(env_dir: Path, task_id: str) -> bool` — renders the task's snapshot into WORKFLOW.md via the existing `render()`; returns False if no snapshot.
- Produces (in `harn/tasks.py`): `Task.step_results: dict = field(default_factory=dict)` serialized as `"step_results"`; `create_task(...)` calls `workflows.snapshot_for_task(env_dir, task.id, workflow)` after saving the task (wrapped in `try/except Exception: pass` — snapshot failure must never fail task creation).

- [ ] **Step 1: Write failing tests** — create `tests/test_task_plan.py`:

```python
"""Per-task workflow snapshot: each task carries its OWN execution plan
(harn_env/tasks/<id>.workflow.json), copied from its preset at creation.
Presets are templates; editing one never touches existing tasks' plans."""
from __future__ import annotations

from harn import tasks, workflows, workflow, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "tasks").mkdir(parents=True)
    workflow.write(env)
    return env


def test_create_task_snapshots_default_plan(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    assert plan is not None
    steps = [n for n in plan["nodes"] if n["kind"] == "step"]
    assert steps and all(n["id"] for n in steps)   # ids stamped


def test_snapshot_is_isolated_from_preset(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    step["agent"] = "cursor"
    workflows.save_task_plan(env, t.id, plan)
    # the default preset/WORKFLOW.md is untouched
    global_parsed = workflow.parse(env)
    assert all(n.get("agent", "") == "" for n in global_parsed["nodes"])
    # and a NEW task doesn't inherit the edit
    t2 = tasks.create_task(env, "Other")
    plan2 = workflows.load_task_plan(env, t2.id)
    assert all(n.get("agent", "") == "" for n in plan2["nodes"])


def test_snapshot_idempotent(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    step["model"] = "opus"
    workflows.save_task_plan(env, t.id, plan)
    again = workflows.snapshot_for_task(env, t.id, None)   # second call
    s2 = next(n for n in again["nodes"] if n["kind"] == "step")
    assert s2["model"] == "opus"   # existing snapshot NOT overwritten


def test_activate_task_renders_snapshot_into_workflow_md(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"] = [n for n in plan["nodes"] if n["kind"] != "step"] + [
        {"kind": "step", "title": "Only step", "body": "do it", "id": "step-aaaaaa",
         "agent": "", "model": "", "effort": "", "temperature": "",
         "required": [], "tools": [], "enabled": True}]
    workflows.save_task_plan(env, t.id, plan)
    assert workflows.activate_task(env, t.id) is True
    assert "Only step" in (env / "WORKFLOW.md").read_text()


def test_step_results_ledger_round_trips(tmp_path):
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    t.step_results["step-aaaaaa"] = {"status": "ok", "tokens": 1234}
    tasks._save(t)
    fresh = tasks.find(env, t.id)
    assert fresh.step_results == {"step-aaaaaa": {"status": "ok", "tokens": 1234}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_task_plan.py -q`
Expected: FAIL (`AttributeError: ... 'load_task_plan'`, `step_results`).

- [ ] **Step 3: Implement**

In `harn/workflows.py` append (uses `json`, `copy` — add `import copy` at top):

```python
# ---------------------------------------------------------------------------
# Per-task plan snapshots — each task's OWN copy of its workflow.
# Presets are templates: create_task copies one here; the engine and the
# studio canvas then read/write ONLY this file for that task.
# ---------------------------------------------------------------------------
def task_plan_path(env_dir: Path, task_id: str) -> Path:
    return env_dir / "tasks" / f"{task_id}.workflow.json"


def load_task_plan(env_dir: Path, task_id: str) -> dict | None:
    p = task_plan_path(env_dir, task_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def save_task_plan(env_dir: Path, task_id: str, parsed: dict) -> Path:
    workflow_mod.ensure_ids(parsed)
    p = task_plan_path(env_dir, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(parsed, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


def snapshot_for_task(env_dir: Path, task_id: str, preset: str | None) -> dict:
    """Copy the task's chosen preset (or the default) into its own plan file.
    Idempotent: an existing snapshot is returned untouched — a task's plan is
    never silently reset by a second call."""
    existing = load_task_plan(env_dir, task_id)
    if existing is not None:
        return existing
    wf = (load(env_dir, preset) if preset else None) or _ensure_default(env_dir)
    plan = copy.deepcopy({"preamble": wf.get("preamble", ""),
                          "nodes": wf.get("nodes", [])})
    workflow_mod.ensure_ids(plan)
    save_task_plan(env_dir, task_id, plan)
    return plan


def activate_task(env_dir: Path, task_id: str) -> bool:
    """Render the task's snapshot into WORKFLOW.md (the one file every chat
    agent reads). The headless engine reads the snapshot directly instead."""
    plan = load_task_plan(env_dir, task_id)
    if plan is None:
        return False
    render(env_dir, {"name": f"task:{task_id}", **plan})
    return True
```

(Check `render()`'s expected dict shape first — `grep -n "def render" harn/workflows.py` — and pass exactly what it needs; it currently takes a `wf` dict with `nodes`/`preamble`.)

In `harn/tasks.py`: add field `step_results: dict = field(default_factory=dict)` next to `stage_checkpoints`; add `"step_results": task.step_results,` to `_to_dict` and `step_results=dict(d.get("step_results") or {}),` to `_from_dict`. In `create_task(...)`, after the task is saved, add:

```python
try:
    from . import workflows as workflows_mod
    workflows_mod.snapshot_for_task(env_dir, task.id, workflow)
except Exception:
    pass   # a failed snapshot must never fail task creation
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_task_plan.py tests/test_workflow.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add harn/workflows.py harn/tasks.py tests/test_task_plan.py
git commit -m "feat(tasks): per-task workflow snapshot + step_results ledger"
```

---

### Task 3: Step-engine helpers in loop.py (pure functions)

**Files:**
- Modify: `harn/loop.py`
- Test: `tests/test_step_engine.py` (create)

**Interfaces:**
- Consumes: node dicts from Task 1 (`id/agent/model/effort/temperature/required/tools/title/body/enabled`); existing helpers `_task_spec`, `_continuity_block`, `_answers_tail`, `_autonomy_note`, `_ASK_GUIDANCE`, `_AUTO_NOTE`, `skills.index`, `tasks.board`, `progress.tail`.
- Produces:
  - `_step_overrides(cfg: Config, step: dict) -> dict` — `{model, effort, temperature}` kwargs for `run_turn`; step's own values win, `model` falls back to `cfg.model`; empty values omitted.
  - `_adapter_for_step(cfg: Config, step: dict, default: Adapter) -> Adapter` — step's `agent` if set and known, else `default`.
  - `_build_step_prompt(env_dir, cfg, task, step, feedback_tail="", auto=False) -> str` — generic per-step prompt.
  - `_INSIGHT_NUDGE: str` — module constant, the save-your-learnings standing instruction.

- [ ] **Step 1: Write failing tests** — create `tests/test_step_engine.py`:

```python
"""Generic step engine helpers: per-step adapter/model resolution and the
one parameterized prompt builder that replaced the six stage-specific ones."""
from __future__ import annotations

from harn import loop, tasks, scaffold, ENV_DIRNAME
from harn.config import Config
from .conftest import make_task


def _step(**kw):
    base = {"kind": "step", "title": "Implement", "body": "One focused change.",
            "id": "step-aaaaaa", "agent": "", "model": "", "effort": "",
            "temperature": "", "required": [], "tools": [], "enabled": True}
    base.update(kw)
    return base


def test_step_overrides_from_node():
    cfg = Config()
    ov = loop._step_overrides(cfg, _step(model="opus", effort="high"))
    assert ov == {"model": "opus", "effort": "high"}


def test_step_overrides_model_falls_back_to_default():
    cfg = Config(model="sonnet")
    assert loop._step_overrides(cfg, _step()) == {"model": "sonnet"}
    assert loop._step_overrides(cfg, _step(model="opus"))["model"] == "opus"


def test_adapter_for_step_uses_step_agent(monkeypatch):
    class A: name = "claude"
    class B: name = "cursor"
    monkeypatch.setattr(loop, "get_adapter",
                        lambda n: B() if n == "cursor" else A())
    cfg = Config()
    assert loop._adapter_for_step(cfg, _step(agent="cursor"), A()).name == "cursor"
    assert loop._adapter_for_step(cfg, _step(), A()).name == "claude"
    # unknown agent name → default, never a crash
    def boom(n): raise ValueError("unknown")
    monkeypatch.setattr(loop, "get_adapter", boom)
    assert loop._adapter_for_step(cfg, _step(agent="nope"), A()).name == "claude"


def test_build_step_prompt_carries_step_and_task(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat",
                  description="## What\nBuild.\n\n## Done when\n- works")
    step = _step(title="Business requirements",
                 body="Interview stakeholders; write the BRD.",
                 required=["standards"], tools=["ask_user"])
    prompt = loop._build_step_prompt(env, Config.load(env), t, step)
    assert "Business requirements" in prompt
    assert "Interview stakeholders" in prompt
    assert "PRJ-001" in prompt
    assert "standards" in prompt          # required skill named
    assert "ask_user" in prompt           # step tool named
    assert loop._INSIGHT_NUDGE.splitlines()[0] in prompt   # insight nudge always on


def test_build_step_prompt_auto_mode_swaps_ask_for_decide(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    step = _step()
    p_human = loop._build_step_prompt(env, Config.load(env), t, step, auto=False)
    p_auto = loop._build_step_prompt(env, Config.load(env), t, step, auto=True)
    assert "AUTONOMOUS MODE" in p_auto and "AUTONOMOUS MODE" not in p_human


def test_build_step_prompt_appends_feedback_tail(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    p = loop._build_step_prompt(env, Config.load(env), t, _step(),
                                feedback_tail="2 tests failed")
    assert "2 tests failed" in p
```

(Confirm `make_task`'s signature in `tests/conftest.py` first and adapt the calls.)

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_step_engine.py -q`
Expected: FAIL (`AttributeError: module 'harn.loop' has no attribute '_step_overrides'`).

- [ ] **Step 3: Implement in loop.py** (add NEW code; do not delete old yet)

```python
_INSIGHT_NUDGE = (
    "## Capture what you learned\n"
    "If this step surfaced a durable convention, trade-off resolution, or "
    "release-worthy change, record it before finishing: `save_to_skill` for "
    "confident conventions, `save_service` for changed service standards, "
    "`record_change` for the release-note line. Skip silently if nothing "
    "durable was learned — do not invent insights."
)


def _step_overrides(cfg: Config, step: dict) -> dict:
    """This step's {model, effort, temperature} kwargs for adapter.run_turn.
    The step's own fields win; a missing model falls back to the global
    default ([harn] model). Empty values are omitted entirely."""
    ov = {}
    for key in ("model", "effort", "temperature"):
        v = str(step.get(key) or "").strip()
        if v:
            ov[key] = v
    if "model" not in ov and cfg.model:
        ov["model"] = cfg.model
    return ov


def _adapter_for_step(cfg: Config, step: dict, default: Adapter) -> Adapter:
    """The adapter that runs this step: its `Agent:` override if set and
    known, else the run's default. Provider-agnostic — any step can run on
    any installed CLI."""
    name = str(step.get("agent") or "").strip()
    if not name or name == default.name:
        return default
    try:
        return get_adapter(name)
    except ValueError:
        return default


def _build_step_prompt(env_dir: Path, cfg: Config, task: tasks.Task,
                       step: dict, feedback_tail: str = "",
                       auto: bool = False) -> str:
    """ONE prompt builder for EVERY workflow step (replaces the six
    stage-specific builders). Structure mirrors _build_prompt: stable
    context first (AGENTS.md, skills index, task spec), the step's own
    instructions in the middle, volatile tail (board/progress/feedback)
    last for prompt-cache reuse."""
    state_dir = env_dir / "state"
    agents_md = env_dir.parent / "AGENTS.md"
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    parts: list[str] = [base]
    if auto:
        parts.append(_AUTO_NOTE)
    if cfg.loop_aware:
        parts.append(_LIFECYCLE_NOTE)
    parts.append(
        "## Available skills (load only what you need)\n"
        "Read a skill via `read_skill` ONLY when needed:\n" + skills.index(env_dir))
    req = [s for s in (step.get("required") or []) if s]
    if req:
        parts.append("## Required skills for THIS step\nLoad these now via "
                     "`read_skill`: " + ", ".join(req))
    parts.append(f"## Current task — {task.id} (status: {task.status})\n"
                 + _task_spec(task))
    tools = [t for t in (step.get("tools") or []) if t]
    parts.append(
        f"## THIS STEP: {step.get('title', '')}\n"
        + (step.get("body") or "").strip()
        + ("\n\nTools for this step: " + ", ".join(tools) if tools else "")
        + "\n\nDo ONLY this step's work, then end your turn — the next step "
          "runs as a separate session with this task's updated state.")
    cont = _continuity_block(task)
    if cont:
        parts.append(cont)
    if not auto:
        parts.append(_autonomy_note(cfg.autonomy))
        parts.append("## Rules\n- " + _ASK_GUIDANCE)
    parts.append(_INSIGHT_NUDGE)
    # volatile tail — keep last (prompt cache)
    if cfg.loop_aware:
        parts.append("## Task board\n" + tasks.board(env_dir))
        prog = progress.tail(env_dir)
        if prog:
            parts.append("## Progress so far\n" + prog)
        answers = _answers_tail(state_dir)
        if answers:
            parts.append("## Earlier answers from the human\n" + answers)
    if feedback_tail:
        parts.append("## Last feedback (tests)\n```\n" + feedback_tail + "\n```")
    return "\n\n".join(p for p in parts if p.strip())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_step_engine.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add harn/loop.py tests/test_step_engine.py
git commit -m "feat(loop): generic step helpers — per-step adapter/model + one prompt builder"
```

---

### Task 4: Rewrite run() as the step engine; delete the fixed pipeline

**Files:**
- Modify: `harn/loop.py` (major), `harn/cli.py` (explain output source)
- Test: `tests/test_step_run.py` (create), existing `tests/test_loop_*.py` updated

**Interfaces:**
- Consumes: Task 2's `workflows.snapshot_for_task/load_task_plan/activate_task`, Task 3's helpers.
- Produces: `run(project_root, env_dir, max_iterations=None, auto=False, only_task=None) -> str` (signature unchanged); per-step ledger writes to `task.step_results[step_id] = {"status": "running"|"ok"|"blocked", "started": iso, "ended": iso|None, "tokens": int|None}`; events `stage_start/stage_end` now emit the step id in the `stage` field and the step TITLE in a new `step_title` field.

Engine semantics (implement exactly):

1. Outer task loop unchanged (`tasks.next_task`, review gate, auto-mode bookkeeping, `_run_end`).
2. On picking up a task: `plan = workflows.snapshot_for_task(env_dir, task.id, task.workflow)`, then `workflows.activate_task(env_dir, task.id)`. NOTE — deliberate deviation from the spec's migration section: the spec said pre-existing in-flight tasks "keep running exactly as they do today", but the old engine is deleted, so that's impossible; snapshot-on-pickup (idempotent, from the task's own preset) is the closest equivalent and is what `snapshot_for_task` gives us for free.
3. `steps = [n for n in plan["nodes"] if n.get("kind") == "step" and n.get("enabled", True) is not False]`.
4. Walk steps in order. Skip any with `task.step_results.get(id, {}).get("status") == "ok"` (resume support). For each remaining step: baseline capture on first real step (existing `set_baseline` logic), `_checkpoint_stage(project_root, task, step_id)`, resolve adapter via `_adapter_for_step`, run `_run_turn(adapter, ..., stage=step_id, ...)` with prompt from `_build_step_prompt`; `_run_turn` gains an optional `overrides: dict | None = None` parameter used INSTEAD of the old `_stage_overrides(cfg, stage)` call (pass `_step_overrides(cfg, step)`).
5. After each step turn: `_handle_block` exactly as today ("resumed" → reload state, re-run SAME step; "blocked" → mark ledger blocked, `_run_end`; "auto" → feedback tail, re-run same step). Then `run_feedback(cfg.test_cmd, ...)`: failing tests → re-run SAME step with feedback tail (each re-run consumes an iteration from `limit`). Tests green → ledger `ok`, next step.
6. After the LAST step: existing submit-for-review + `_review_gate` + `_auto_changelog` block unchanged.
7. DELETE: `PIPELINE`, `Stage` dataclass, `active_stages`, `_stage_overrides`, `_adapter_for_stage`, `_build_planning_prompt`, `_build_prompt`, `_build_verify_prompt`, `_build_ui_verify_prompt`, `_build_reconcile_prompt` (fold its text into a module constant used by `reconcile_headless`), `_run_verify`, `_run_ui_verify`, `_run_oracle`, `_run_reconcile`, `_verify_verdict`, `_ui_verdict`, `_ui_applicable`, `_VERIFY_INSTRUCTIONS`, the planning-funnel block, `_MAX_PLAN_TURNS`, `tests_nudged`/`plan_turns` locals. KEEP: `oracle_review` + `reconcile_headless` + `_pick_oracle_adapter` + `_oracle_instructions`/`_oracle_verdict`/`_build_oracle_prompt` (used by `watch()`); replace their `_stage_overrides(cfg, "oracle"/"reconcile")` calls with `{"model": cfg.model} if cfg.model else {}`.
8. `explain(cfg)` → `explain(env_dir, cfg)`: list the ACTIVE workflow's enabled steps (`workflow.parse(env_dir)`), one line each: `N. [✓/·] title (agent or default / model or default)`. Update its caller in `harn/cli.py` (`grep -n "explain" harn/cli.py`).

- [ ] **Step 1: Write the integration tests** — create `tests/test_step_run.py` with a `RecordingAdapter` (copy the pattern from the current `tests/test_stage_models.py`, keeping its `run_turn(prompt, cwd, timeout=1800, *, model=None, effort=None, temperature=None)` signature):

```python
"""The step engine end-to-end: harn run walks the TASK'S OWN plan —
arbitrary step count, per-step agent/model, resume, checkpoints."""
from __future__ import annotations

from harn import loop, tasks, workflows, scaffold, gitutil, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task
import subprocess


class RecordingAdapter:
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt, "model": model, "effort": effort})
        return AgentResult(ok=True, text="done")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _project(tmp_path, n_steps=10):
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 40\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    plan = {"preamble": "", "nodes": [
        {"kind": "step", "title": f"Step {i}", "body": f"do part {i}",
         "id": f"step-{i:06x}", "agent": "", "model": "", "effort": "",
         "temperature": "", "required": [], "tools": [], "enabled": True}
        for i in range(1, n_steps + 1)]}
    workflows.save_task_plan(env, t.id, plan)
    return env, t


def test_engine_runs_every_enabled_step_in_order(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=10)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 10
    for i, c in enumerate(fake.calls, 1):
        assert f"Step {i}" in c["prompt"]


def test_per_step_model_and_agent_route_correctly(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=3)
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"][0]["model"] = "opus"
    plan["nodes"][1]["agent"] = "other"
    workflows.save_task_plan(env, t.id, plan)
    fake, other = RecordingAdapter(), RecordingAdapter()
    other.name = "other"
    reg = {"fake": fake, "other": other}
    monkeypatch.setattr(loop, "get_adapter", lambda n: reg.get(n, fake))
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert fake.calls[0]["model"] == "opus"      # step 1 override
    assert len(other.calls) == 1                 # step 2 ran on 'other'
    assert "Step 2" in other.calls[0]["prompt"]


def test_ledger_records_ok_and_resume_skips_done_steps(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=4)
    t = tasks.find(env, t.id)
    t.step_results["step-000001"] = {"status": "ok"}
    tasks._save(t)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 3                  # step 1 skipped
    assert "Step 2" in fake.calls[0]["prompt"]
    fresh = tasks.find(env, t.id)
    assert all(fresh.step_results[f"step-{i:06x}"]["status"] == "ok"
              for i in range(1, 5))


def test_disabled_step_is_skipped(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=3)
    plan = workflows.load_task_plan(env, t.id)
    plan["nodes"][1]["enabled"] = False
    workflows.save_task_plan(env, t.id, plan)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert len(fake.calls) == 2
    assert "Step 2" not in "".join(c["prompt"] for c in fake.calls)


def test_checkpoint_per_step_id(tmp_path, monkeypatch):
    env, t = _project(tmp_path, n_steps=2)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    fresh = tasks.find(env, t.id)
    assert set(fresh.stage_checkpoints) == {"step-000001", "step-000002"}
```

- [ ] **Step 2: Run to verify failures** — `python3 -m pytest tests/test_step_run.py -q` → FAIL (engine still walks six stages).

- [ ] **Step 3: Rewrite run() + deletions per the Interfaces block above.** The inner body replaces everything between the task pickup and the submit-for-review block; the submit/review/auto tail is kept verbatim. `_run_turn` change:

```python
def _run_turn(adapter, env_dir, prompt, project_root, *, task_id, stage,
              tok_totals, tok_costs, cfg, verdict=None, overrides=None,
              step_title=None):
    ...
    events.emit(env_dir, "stage_start", task_id=task_id, stage=stage,
                agent=adapter.name, step_title=step_title)
    ...
    res = adapter.run_turn(prompt, project_root, **(overrides or {}))
    ...
    events.emit(env_dir, "stage_end", ..., step_title=step_title, ...)
```

Ledger writes (in the engine, around each step turn; skipped when `auto` — auto mode never mutates task files):

```python
if not auto:
    task.step_results[sid] = {"status": "running",
                              "started": tasks._now_iso(), "ended": None}
    tasks._save(task)
...after success...
if not auto:
    task.step_results[sid] = {"status": "ok", "started": started,
                              "ended": tasks._now_iso(),
                              "tokens": res.total_tokens}
    tasks._save(task)
```

- [ ] **Step 4: Run the new tests + step-engine tests** — `python3 -m pytest tests/test_step_run.py tests/test_step_engine.py tests/test_task_plan.py -q` → PASS.

- [ ] **Step 5: Triage the full suite.** Run `python3 -m pytest -q 2>&1 | tail -25`. Update tests that encoded the OLD engine but still test LIVE behavior (e.g. `test_loop_hil.py`, `test_loop_review.py`, `test_watch.py` — these should still pass or need only mechanical fixes like removed kwargs). DELETE tests of removed machinery: `tests/test_stage_models.py` (engine part), verify/oracle/ui_verify/reconcile stage tests wherever they live (`grep -rln "_run_verify\|_run_oracle\|_build_verify_prompt\|VERIFY: PASS" tests/`). Do NOT delete `tests/test_watch.py`'s `oracle_review` tests — they must pass unchanged.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(loop)!: step engine — run walks the task's own plan; fixed six-stage pipeline removed"
```

---

### Task 5: Per-step Run/Rerun by id (run_step, CLI, runner, studio backend)

**Files:**
- Modify: `harn/loop.py` (`run_stage` → `run_step`), `harn/cli.py` (`--stage` → `--step`), `harn/runner.py` (param rename), `harn/studio.py` (backend `launch_stage` → `launch_step`, route `/api/tasks/run_step`)
- Test: rewrite `tests/test_run_stage.py` → `tests/test_run_step.py`, update `tests/test_runner.py`, `tests/test_stage_launch.py` → `tests/test_step_launch.py`, keep `tests/test_stage_checkpoints.py` (rename keys to step ids)

**Interfaces:**
- Produces: `loop.run_step(project_root, env_dir, task_id, step_id, *, rerun=False) -> dict` — loads the task plan, finds the step by id (error dict if unknown), on `rerun=True` restores `task.stage_checkpoints[step_id]` via `gitutil.rollback_to`, checkpoints, runs ONE turn with `_build_step_prompt`/`_adapter_for_step`/`_step_overrides`, updates the ledger, returns `{"ok": bool, "step_id": ..., "title": ...}`.
- CLI: `harn run --task ID --step STEP_ID [--rerun]`.
- `runner.launch(..., step: str | None = None, rerun: bool = False)` builds `--step`; pid-file info key `"step"`.
- Studio: POST `/api/tasks/run_step` `{task_id, step_id, rerun}` → `launch_step`; `rerun_workflow` unchanged (whole-task rollback already generic).

- [ ] **Step 1: Write failing tests.** `tests/test_run_step.py` mirrors the existing `test_run_stage.py` structure (same git fixture helpers) with: unknown step id → `{"ok": False}`; single run executes exactly one turn with that step's prompt; `rerun=True` first restores the checkpoint (write a file, checkpoint via engine run, dirty the file, rerun step, assert file content restored — port `test_rerun_discards_previous_attempts_edit` keyed by step id). Update `tests/test_runner.py`'s `--stage` assertions to `--step`.

- [ ] **Step 2: Run to verify failures** — `python3 -m pytest tests/test_run_step.py tests/test_runner.py -q`.

- [ ] **Step 3: Implement.** `run_step` is a focused rewrite of the current `run_stage` (`harn/loop.py:1331`): replace the `MODEL_STAGES` validation with plan lookup:

```python
plan = workflows.load_task_plan(env_dir, task_id) \
    or workflows.snapshot_for_task(env_dir, task_id, task.workflow)
step = next((n for n in plan["nodes"]
             if n.get("kind") == "step" and n.get("id") == step_id), None)
if step is None:
    return {"ok": False, "error": f"unknown step id {step_id!r}"}
```

Keep the rerun-restore/checkpoint/turn skeleton; prompt/adapter/overrides come from Task 3's helpers. CLI/runner/studio are mechanical renames — follow the existing `--stage` plumbing found via `grep -n "stage" harn/cli.py harn/runner.py | grep -v message`.

- [ ] **Step 4: Run tests** — the three rewritten files + full suite triage. Expected leftover failures only in studio-UI tests (Task 6).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: per-step Run/Rerun by step id (CLI --step, runner, studio API)"
```

---

### Task 6: Studio UI — agent/model on every step; task plan editing + live progress

**Files:**
- Modify: `harn/studio.py` (frontend `<script>` + backend payload helpers)
- Test: `tests/test_studio_models.py` → keep only `save_defaults`/`models_payload`-defaults tests; extend `tests/test_task_plan.py` with the two backend routes; live browser check via Claude Preview.

**Interfaces:**
- Backend produces: GET `/api/task_plan?task=<id>` → the task's plan JSON (`{"ok": True, "plan": {...}, "task_id": ...}` or `{"ok": False}`); POST `/api/task_plan` `{task_id, plan}` → `save_task_plan` + `{"ok": True}`. `models_payload` drops `values`/`stages` (no more `[models.*]`), keeps `agents` capability map + `default_agent`/`default_model`; `save_models` (POST `/api/models`) is DELETED.
- Frontend produces: per-step inspector edits `n.agent/n.model/n.effort/n.temperature` directly on the node (saved with the flow via the existing save path — `/api/workflow` for presets, `/api/task_plan` when a task plan is open); dropdowns populated from `MODELS.agents[chosen agent]` with the existing `selectOrCustom` Custom… escape hatch; the "Runs as"/stage dropdown, `nodeStage`, `STAGE_KW`, `effectiveStage`, `RUN_STAGES` and all `MODEL_STAGES`-derived gating are deleted; Run/Rerun buttons appear on EVERY step, posting `{task_id, step_id: n.id, rerun}` to `/api/tasks/run_step`; Board task detail gets an "Edit this task's plan" button that loads `/api/task_plan` into the canvas (a `PLAN_MODE = {taskId}` global switches the save target and shows a banner "editing plan of PRJ-001 — changes affect only this task"); `applyProgress` maps `stage_start/stage_end` events to nodes by `n.id === ev.stage`.

- [ ] **Step 1: Backend routes + payload trim, with tests first.** Add to `tests/test_task_plan.py`:

```python
def test_task_plan_roundtrip_via_studio(tmp_path):
    from harn import studio
    env = _env(tmp_path)
    t = tasks.create_task(env, "Add auth")
    payload = studio.task_plan_payload(env, t.id)
    assert payload["ok"] and payload["plan"]["nodes"]
    plan = payload["plan"]
    step = next(n for n in plan["nodes"] if n["kind"] == "step")
    step["model"] = "opus"
    r = studio.save_task_plan_route(env, {"task_id": t.id, "plan": plan})
    assert r["ok"] is True
    assert next(n for n in workflows.load_task_plan(env, t.id)["nodes"]
                if n["kind"] == "step")["model"] == "opus"
```

Implement `task_plan_payload(env_dir, task_id)` and `save_task_plan_route(env_dir, payload)` in `harn/studio.py` (thin wrappers over `workflows.load_task_plan`/`snapshot_for_task`/`save_task_plan`), wire GET/POST routes, delete `save_models` + its route + the `values`/`stages` keys from `models_payload`. Trim `tests/test_studio_models.py` to the surviving functions.

- [ ] **Step 2: Frontend rework.** In the `<script>`: delete `STAGE_KW`/`nodeStage`/`effectiveStage`/`RUN_STAGES`/`setNodeStage`/`stageDropdown` and the `MODELS.values` plumbing (`setModelField`/`setStageAgent`/`saveStepModel` → replaced by direct `selNode.agent = …; checkDirty()` setters). Inspector model section (always rendered for steps):

```javascript
const agentOpts=`<option value="">Default: ${esc(AGENT_LABEL[MODELS.default_agent]||MODELS.default_agent||'(unset)')}</option>`
  +allAgentNames().map(a=>`<option value="${esc(a)}" ${n.agent===a?'selected':''}>${esc(AGENT_LABEL[a]||a)}${agentCaps(a).available?'':' (not installed)'}</option>`).join('');
// modelChoices now reads the STEP's agent:
function stepChoices(n){const c=agentCaps(n.agent||MODELS.default_agent||'');return {models:c.models||[],efforts:c.efforts||[],temperatures:c.temperatures||[]};}
```

`selectOrCustom` is re-pointed at node fields: `onchange` handlers call `setStepField('model', v)` which does `selNode.model=v; checkDirty(); renderInsp();` (agent change also clears an invalid model, same guard as the current `setStageAgent`). Run/Rerun: `runStep(n.id)` / `rerunStep(n.id)` post to `/api/tasks/run_step`; the buttons render on every step node (`.has-run` class now unconditional for steps with an id). `applyProgress`: replace the `effectiveStage` lookup with `by-id` matching. Add `PLAN_MODE` (null | `{taskId}`), the Board button, the banner, and the save-path switch in `saveFlow()`.

- [ ] **Step 3: JS syntax check**

```bash
python3 - <<'EOF'
import re
text = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', text, re.S)
open('/tmp/studio_check.js', 'w').write(m.group(1))
EOF
node --check /tmp/studio_check.js
```

Expected: exit 0.

- [ ] **Step 4: Full pytest** — `python3 -m pytest -q` → green (all prior tasks' tests + reworked studio tests).

- [ ] **Step 5: Live browser verification** (repo pattern): scratch project in /tmp, `harn setup`, temp `.claude/launch.json` running `python3 -m harn.cli ui <proj> --port 8765 --no-open`, Claude Preview: (1) every step's inspector shows Agent&Model controls with dropdowns, no stage dropdown anywhere; (2) create a task on the Board → "Edit this task's plan" opens the canvas with the banner; set a step's agent to another CLI and Save; confirm the preset file unchanged and `tasks/<id>.workflow.json` updated; (3) Run button on an arbitrary middle step posts `run_step` with that node's id (inspect via preview_network). Stop the server, delete the temp project and launch.json.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(studio): agent/model on every step; per-task plan editing; run/rerun by step id"
```

---

### Task 7: Config cleanup, docs, version 0.14.0

**Files:**
- Modify: `harn/config.py`, `harn/__init__.py`, `pyproject.toml`, `README.md` (models section), `harn/templates/harn.toml` (if it carries `[models.*]` examples — check with `grep -rn "models\." harn/templates/`)
- Test: `tests/test_stage_models.py` deleted; config tests updated in place

**Interfaces:** none new — pure removal.

- [ ] **Step 1: Find every remaining reference**

```bash
grep -rn "MODEL_STAGES\|stage_models\|_parse_stage_models" harn/ tests/ --include="*.py" | grep -v __pycache__
```

Expected: only `harn/config.py` (+ possibly stale test files). Anything else = a missed rename in Tasks 4–6; fix it there first.

- [ ] **Step 2: Delete from config.py:** `MODEL_STAGES`, `_parse_stage_models`, the `stage_models` field + its `Config.load` line + its docstring block. Check the four gate flags: `grep -rn "cfg.planning\|cfg.verify\b\|cfg.oracle\b\|cfg.browser_enabled" harn/ --include="*.py" | grep -v config.py` — delete from `Config`/`DEFAULTS` ONLY the flags with zero remaining references (`planning` and `verify` are expected dead; `oracle`/`oracle_agent` stay if `watch()` still reads them; `browser.*` stays for UI-step tool context).

- [ ] **Step 3: Delete `tests/test_stage_models.py` and any other file the Step 1 grep flagged as stale. Bump `__version__` and pyproject to `0.14.0`. Update README's per-stage-models section to describe per-step fields in WORKFLOW.md + the Settings defaults.**

- [ ] **Step 4: Full suite + JS check** — `python3 -m pytest -q` green; `node --check` (Task 6 Step 3 command) exit 0.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat!: 0.14.0 — arbitrary workflow steps; remove [models.<stage>] config"
```
