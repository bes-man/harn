# Parallel Workflow Steps (Phase 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Steps placed at the same horizontal level on the studio canvas execute concurrently under `harn run`, each isolated in its own git worktree, merging back into the main tree as patches (no commits), with per-step independent rollback and a single shared task context — regardless of which agent CLI each step uses.

**Architecture:** A new `parallel` field on workflow-plan step nodes (parsed/composed like every other Phase 1/2 field) groups consecutive same-group steps into a "wave." `harn run`'s step walk detects a wave, checkpoints the current tree via the existing `gitutil.checkpoint`, creates one `git worktree` per step off that checkpoint, replicates every agent-connector file into each worktree with an absolutized `HARN_ENV_DIR` (the provider-agnostic guarantee), runs all steps concurrently via a thread pool, captures each finished step's diff as a patch (hidden ref, same pinning trick as existing checkpoints), then applies the patches to the main tree sequentially — a conflict dispatches one agent-merge turn. Independent rollback reverse-applies one step's patch, falling back to whole-wave revert if that's no longer possible. The studio canvas derives the `parallel` grouping from node Y-position on drag-end and renders a "lane" backdrop.

**Tech Stack:** Python stdlib only (existing constraint: `subprocess`, `concurrent.futures.ThreadPoolExecutor`, `tempfile`), pytest with real git repos, single-file vanilla-JS studio (`harn/studio.py`).

**Spec:** `docs/superpowers/specs/2026-07-10-parallel-workflow-steps-design.md`

## Global Constraints

- Provider-agnostic: a wave's steps may use different `Agent:` values; nothing in the wave/worktree/merge/rollback code branches on which CLI a step uses. Verified by a test with two steps on different (faked) agents in the same wave.
- Stdlib-only in `harn/` (no new dependencies).
- harn NEVER creates git commits — merged wave results land in the working tree as an uncommitted diff, same invariant as every prior phase.
- `On fail:` set on a parallel-grouped step is a config error (log `config_error`, treat as unset) — no retry/handler semantics inside a wave (Phase 3 non-goal per spec).
- A wave of size 1 (a step whose `parallel` group has no other CONSECUTIVE member in the ordered step list) runs through the EXISTING single-step path unchanged — no worktree, no thread pool, zero behavior change for non-parallel plans. This must hold for both agent- and command-type steps.
- Every commit ends with the trailer: `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
- Run the full suite with `python3 -m pytest -q` from the repo root (baseline: 440 passed at plan-writing time). JS syntax check via extracting the `<script>` block from `harn/studio.py` and running `node --check` (established pattern).
- Bump `harn/__init__.py`'s `__version__` and `pyproject.toml`'s `version` on every commit that changes `harn/` (current: 0.16.2) — per standing project rule, so `harn update` always proves a change landed. A single-file mechanical fix bumps the patch digit; a task's feature-sized commit bumps minor.
- Any real subprocess/worktree/thread test must clean up after itself (no leftover `git worktree` entries, no lingering threads) even when the test's own assertion fails — use `try/finally` or a fixture, not bare sequential calls.

---

### Task 1: `workflow.py` — `Parallel:` field (parse/compose)

**Files:**
- Modify: `harn/workflow.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: nothing new (works alongside `id/agent/model/effort/temperature/type/command/on_fail` from Phases 1-2).
- Produces: `parse()` node dicts (`kind == "step"`) gain `parallel: str` (`""` default). `compose()` writes a `Parallel: <group>` line when non-empty, using the EXACT same convention as `Type:`/`Command:` (a plain value line, no title-resolution needed — unlike `On fail:`, the group id has no rename-survival concern since it's not a reference to another step).

- [ ] **Step 1: Write the failing tests.** Add to `tests/test_workflow.py`, immediately after the Phase 2 `Type:`/`Command:`/`On fail:` test block:

```python
# --- parallel steps (Phase 3) -------------------------------------------- #

def test_parse_defaults_parallel_to_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    for n in parsed["nodes"]:
        if n["kind"] == "step":
            assert n["parallel"] == ""


def test_parallel_round_trips_through_compose(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    implement = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    verify = next(n for n in parsed["nodes"] if n["title"] == "Verify")
    implement["parallel"] = "wave-1"
    workflow.save_parsed(env, parsed)

    reparsed = workflow.parse(env)
    i2 = next(n for n in reparsed["nodes"] if n["title"] == "Implement")
    assert i2["parallel"] == "wave-1"
    v2 = next(n for n in reparsed["nodes"] if n["title"] == "Verify")
    assert v2["parallel"] == ""


def test_compose_writes_parallel_line(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    step = next(n for n in parsed["nodes"] if n["title"] == "Implement")
    step["parallel"] = "wave-1"
    text = workflow.compose(env, parsed)
    assert "Parallel: wave-1" in text


def test_parallel_omitted_when_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    workflow.write(env)
    parsed = workflow.parse(env)
    text = workflow.compose(env, parsed)
    assert "Parallel:" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_workflow.py -q -k parallel`
Expected: FAIL (`KeyError: 'parallel'`).

- [ ] **Step 3: Implement in `harn/workflow.py`.**

(a) Add a regex near `_ONFAIL_RE`:
```python
_PARALLEL_RE = re.compile(r"^\s*Parallel:\s*(\S+)\s*$", re.IGNORECASE)
```

(b) In `parse()`'s node-init dict, add `"parallel": ""` alongside the existing keys.

(c) In `parse()`'s line-matching chain, insert a block right after the `onfail_m` block:
```python
parallel_m = _PARALLEL_RE.match(line)
if parallel_m:
    cur["_decl"] = True
    cur["parallel"] = parallel_m.group(1).strip()
    continue
```

(d) In `compose()`'s five/six-field loop tuple, add `("parallel", "Parallel")` as one more entry — it's a plain value field like `type`/`command`, needs no id-resolution, so it slots into the SAME loop as those two (not the separate `on_fail` title-resolution block below it).

(e) Update `parse()`'s docstring to document the new field.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_workflow.py -q`
Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q 2>&1 | tail -5`
Expected: fully green (this task only adds a field with a safe empty default).

- [ ] **Step 6: Bump version + commit**

Bump `harn/__init__.py` and `pyproject.toml` from `0.16.2` to `0.17.0` (new feature groundwork).

```bash
git add harn/workflow.py tests/test_workflow.py harn/__init__.py pyproject.toml
git commit -m "feat(workflow): Parallel: step field (Phase 3); version 0.17.0"
```

---

### Task 2: `gitutil.py` — worktree + patch plumbing

**Files:**
- Modify: `harn/gitutil.py`
- Test: `tests/test_gitutil_worktree.py` (create)

**Interfaces:**
- Consumes: existing `_run`, `is_repo`, `head`, `checkpoint` (unchanged).
- Produces four new functions:
  - `create_worktree(cwd: Path, base_ref: str, worktree_path: Path) -> bool` — `git worktree add --detach <worktree_path> <base_ref>`; returns whether it succeeded (never raises — best-effort like the rest of this module).
  - `remove_worktree(cwd: Path, worktree_path: Path) -> None` — `git worktree remove --force <worktree_path>` (best-effort; also handles the case the directory was already deleted).
  - `diff_as_patch(worktree_path: Path, base_ref: str) -> str` — `git -C <worktree_path> diff <base_ref>` (full unified diff of the worktree against the base, working tree vs base_ref, including untracked-but-added files — use `git add -A` inside the worktree first so untracked new files appear in the diff, then `git diff --cached <base_ref>` for a complete patch covering staged+working changes relative to base). Returns `""` if there's no diff.
  - `apply_patch(cwd: Path, patch_text: str, *, reverse: bool = False) -> bool` — pipes `patch_text` to `git apply --3way` (`--reverse` too if `reverse=True`) via stdin; returns whether it applied cleanly (exit code 0). Never raises.
  - `save_patch_ref(cwd: Path, task_id: str, step_id: str, patch_text: str) -> None` / `load_patch_ref(cwd: Path, task_id: str, step_id: str) -> str` — patches are small text; store them as blobs via `git hash-object -w --stdin` + `update-ref refs/harn/patches/<task_id>/<step_id> <blob-sha>` (save), and `git cat-file -p <ref>` (load). Mirrors `checkpoint`'s hidden-ref pinning trick, but for blob objects (a patch is text content, not a tree/commit state) — confirm `git cat-file -p` on a blob ref returns its raw content before committing to this approach; if a blob-via-hash-object doesn't resolve cleanly through a plain ref name in your git version, fall back to a plain file under `env_dir` instead (e.g. `harn_env/state/patches/<task_id>/<step_id>.patch`) and say so in your report — either storage is fine, the INTERFACE (`save_patch_ref`/`load_patch_ref`) is what matters to callers.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_gitutil_worktree.py`:

```python
"""Worktree + patch plumbing for parallel workflow steps (Phase 3): isolate
each parallel step in its own worktree off a shared checkpoint, capture its
diff as a patch, and apply/reverse-apply that patch against the main tree."""
from __future__ import annotations

import subprocess
from pathlib import Path

from harn import gitutil


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "app.py").write_text("original\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "baseline"], tmp_path)
    return tmp_path


def test_create_and_remove_worktree(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    assert gitutil.create_worktree(root, base_ref, wt) is True
    assert (wt / "app.py").read_text() == "original\n"
    gitutil.remove_worktree(root, wt)
    assert not wt.exists()


def test_diff_as_patch_captures_worktree_changes(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("changed by step A\n")
    (wt / "new_file.py").write_text("brand new\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    assert "changed by step A" in patch
    assert "new_file.py" in patch
    gitutil.remove_worktree(root, wt)


def test_diff_as_patch_empty_when_no_changes(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    assert gitutil.diff_as_patch(wt, base_ref) == ""
    gitutil.remove_worktree(root, wt)


def test_apply_patch_applies_cleanly_to_main_tree(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("changed by step A\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    gitutil.remove_worktree(root, wt)

    assert gitutil.apply_patch(root, patch) is True
    assert (root / "app.py").read_text() == "changed by step A\n"


def test_apply_patch_reverse_undoes_it(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("changed by step A\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    gitutil.remove_worktree(root, wt)
    gitutil.apply_patch(root, patch)

    assert gitutil.apply_patch(root, patch, reverse=True) is True
    assert (root / "app.py").read_text() == "original\n"


def test_apply_patch_conflict_reports_false_not_raise(tmp_path):
    root = _repo(tmp_path)
    base_ref = gitutil.checkpoint(root, "T1", "wave-1")
    wt = tmp_path / "wt1"
    gitutil.create_worktree(root, base_ref, wt)
    (wt / "app.py").write_text("step A's version\n")
    patch = gitutil.diff_as_patch(wt, base_ref)
    gitutil.remove_worktree(root, wt)
    # main tree ALREADY has a conflicting change on the same line
    (root / "app.py").write_text("someone else's incompatible edit\n")

    assert gitutil.apply_patch(root, patch) is False
    # main tree is untouched by the failed attempt
    assert (root / "app.py").read_text() == "someone else's incompatible edit\n"


def test_save_and_load_patch_ref(tmp_path):
    root = _repo(tmp_path)
    gitutil.save_patch_ref(root, "T1", "step-a", "diff --git a/x b/x\n+hello\n")
    assert gitutil.load_patch_ref(root, "T1", "step-a") == "diff --git a/x b/x\n+hello\n"


def test_load_patch_ref_missing_returns_empty(tmp_path):
    root = _repo(tmp_path)
    assert gitutil.load_patch_ref(root, "T1", "nope") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_gitutil_worktree.py -q`
Expected: FAIL (functions don't exist yet).

- [ ] **Step 3: Implement in `harn/gitutil.py`** per the Interfaces section. Read the existing `checkpoint`/`rollback_to`/`_run` functions first to match this module's exact style (best-effort, never raises, returns falsy/empty on any git failure).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_gitutil_worktree.py -q`
Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q 2>&1 | tail -5`
Expected: fully green (this task only adds new functions to gitutil.py, doesn't change any existing one).

- [ ] **Step 6: Bump version + commit**

```bash
git add harn/gitutil.py tests/test_gitutil_worktree.py harn/__init__.py pyproject.toml
git commit -m "feat(gitutil): worktree + patch plumbing for parallel steps (Phase 3); version 0.17.1"
```

---

### Task 3: Concurrency-safety audit — lock every harn_env write path

**Files:**
- Modify: `harn/mcp_server.py`, possibly `harn/skills.py`/`harn/tasks.py` (wherever the actual file write lives)
- Test: `tests/test_concurrent_writes.py` (create)

**Interfaces:**
- Consumes: existing `tasks._claim_lock(env_dir)` (a generic cross-process `fcntl`-based advisory lock, currently only used for atomic task claiming).
- Produces: every MCP tool that writes into `harn_env` and could plausibly be called by TWO parallel-wave agents at the same moment is wrapped in `with tasks._claim_lock(env_dir):` around its actual file write (not the whole tool body if that body does slow I/O like reading a PRD — lock scope should be as narrow as correctness allows). Audit list (confirm each by reading the function, don't assume): `save_to_skill`, `save_service`, `record_decision`, `set_scratchpad`, `record_change` (changelog), `submit_for_review`/`request_changes` if reachable mid-wave, and the task ledger `_save`/`step_results` write path already used by the wave executor itself (Task 5 will call this directly from multiple threads — confirm it's covered here too, since it's the SAME lock).

- [ ] **Step 1: Read every candidate function's current body** (`grep -n "def save_to_skill\|def save_service\|def record_decision\|def set_scratchpad\|def record_change" harn/mcp_server.py` and follow each to its actual file-write call) and list, in your report, which ones currently have ZERO locking (expected: all of them, since `_claim_lock` today only guards task-claim atomicity).

- [ ] **Step 2: Write the failing test.** Create `tests/test_concurrent_writes.py`:

```python
"""Two threads calling the same harn_env-writing MCP tool concurrently must
not corrupt the file or lose an entry — this is new as of Phase 3, where
multiple real agent processes can call these tools at the same instant."""
from __future__ import annotations

import threading
from pathlib import Path

from harn import scaffold, skills, ENV_DIRNAME


def _env(tmp_path):
    scaffold.setup(tmp_path)
    return tmp_path / ENV_DIRNAME


def test_concurrent_save_to_skill_does_not_lose_entries(tmp_path, monkeypatch):
    env = _env(tmp_path)
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    from harn import mcp_server as ms
    srv = ms.build_server(start_watch=False)
    save_fn = next(t.fn for t in srv._tool_manager._tools.values()
                   if t.name == "save_to_skill")

    errors = []
    def worker(i):
        try:
            save_fn(skill="concurrency-test", content=f"entry-{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors
    body = (env / "skills" / "concurrency-test" / "SKILL.md").read_text()
    for i in range(10):
        assert f"entry-{i}" in body, f"entry-{i} lost to a concurrent-write race"
```

(Adapt the exact skill-file path/lookup to however `skills.append_learning`/`save_to_skill` actually lays it out — check `harn/skills.py` first; the test's job is proving no entry is lost, adjust the read-back path to match reality.)

- [ ] **Step 3: Run to verify it fails (or is flaky) without locking**

Run: `python3 -m pytest tests/test_concurrent_writes.py -q -k skill` (run it 3-5 times in a row if it doesn't fail deterministically — file-write races are often nondeterministic; note the observed behavior in your report either way).

- [ ] **Step 4: Wrap each audited write path in `tasks._claim_lock(env_dir)`.** For `save_to_skill`/`save_service`/`record_decision`/`set_scratchpad`/`record_change`, this likely means wrapping the call into `skills.append_learning`/the task `_save()` call/etc. — the lock must cover the READ-MODIFY-WRITE cycle (read current content, append, write back), not just the final write, or the race stays possible.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_concurrent_writes.py -q`
Expected: passes reliably (run it 5 times to build confidence against flakiness: `for i in 1 2 3 4 5; do python3 -m pytest tests/test_concurrent_writes.py -q || break; done`).

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest -q 2>&1 | tail -5`
Expected: fully green — adding a lock around an already-sequential single-process call path changes nothing observable for existing tests.

- [ ] **Step 7: Bump version + commit**

```bash
git add harn/mcp_server.py harn/skills.py harn/tasks.py tests/test_concurrent_writes.py harn/__init__.py pyproject.toml
git commit -m "fix: lock harn_env writes against concurrent parallel-step agents (Phase 3 prep); version 0.17.2"
```

---

### Task 4: `loop.py` — wave detection + worktree setup + concurrent execution

**Files:**
- Modify: `harn/loop.py`
- Test: `tests/test_parallel_waves.py` (create)

**Interfaces:**
- Consumes: Task 1's `parallel` field; Task 2's `gitutil.create_worktree`/`remove_worktree`/`diff_as_patch`/`save_patch_ref`; existing `_adapter_for_step`, `_build_step_prompt`, `_step_overrides`, `_run_turn`, `_checkpoint_stage`, `run_feedback` (for command-type wave members).
- Produces:
  - `_collect_wave(steps: list[dict], first: dict) -> list[dict]` — pure function: if `first["parallel"]` is empty, returns `[first]`. Else walks forward from `first`'s position in `steps` while the immediately-following entries share the SAME `parallel` value (contiguous run only — a same-group id reappearing after a different step in between starts a NEW wave, per spec).
  - `_replicate_connectors(project_root: Path, worktree_path: Path, env_dir: Path) -> None` — copies whichever of `.mcp.json`, `.cursor/mcp.json`, `AGENTS.md`, `CLAUDE.md` exist at `project_root` into the same relative paths under `worktree_path`, and for `.mcp.json`/`.cursor/mcp.json` copies, rewrites the `harn` server's `env.HARN_ENV_DIR` to `str(env_dir.resolve())` (absolute) before writing — this is THE provider-agnostic guarantee, test it explicitly for both connector file shapes.
  - `_run_parallel_wave(env_dir: Path, project_root: Path, task: "tasks.Task", wave: list[dict], cfg: Config, default_adapter) -> str` — returns `"advance"` (all merged clean), `"blocked"` (a member hit `ask_user`), or `"conflict_unresolved"` (merge agent couldn't resolve and escalated — treat like blocked for the caller). Orchestrates: checkpoint → per-step worktree + connector replication → `ThreadPoolExecutor` running each step (agent-turn via `_run_turn` with `cwd=worktree`, or command-type via `run_feedback(command, cwd=worktree)`, mirroring `_run_command_step`'s ledger semantics but writing into the worktree) → patch capture per finished step → worktree removal → hand off to Task 5's merge function.

Prompt addition for parallel-wave agent steps (per spec's "Blocking inside a wave" section): `_build_step_prompt` gains a `parallel_note: str = ""` parameter (default `""`, backward compatible), and `_run_parallel_wave` passes a fixed note telling the agent it's running concurrently with N sibling steps and should decide autonomously (record via `record_decision`) rather than call `ask_user`, unless truly blocked.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_parallel_waves.py`:

```python
"""Parallel workflow steps (Phase 3): consecutive same-`parallel`-group steps
run concurrently, each isolated in its own git worktree off a shared
checkpoint, with every agent connector replicated in so ANY provider's CLI
finds the harn MCP server and writes into the ONE shared task context."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harn import loop, tasks, workflows, scaffold, gitutil, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _step(title, **kw):
    base = {"kind": "step", "title": title, "body": "", "id": "", "agent": "",
            "model": "", "effort": "", "temperature": "", "type": "",
            "command": "", "on_fail": "", "parallel": "", "required": [],
            "tools": [], "enabled": True}
    base.update(kw)
    return base


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


class WritingAdapter:
    """Writes a DISTINCT file per call (keyed by cwd's basename) so two
    concurrent steps never touch the same file — proves real isolation."""
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt, "cwd": str(cwd)})
        out_file = Path(cwd) / f"output_{Path(cwd).name}.txt"
        out_file.write_text(f"done in {Path(cwd).name}\n")
        return AgentResult(ok=True, text="did the work")


def test_collect_wave_groups_consecutive_same_parallel_id():
    steps = [_step("A", id="s1"), _step("B", id="s2", parallel="wave-1"),
             _step("C", id="s3", parallel="wave-1"), _step("D", id="s4")]
    assert [s["id"] for s in loop._collect_wave(steps, steps[0])] == ["s1"]
    assert [s["id"] for s in loop._collect_wave(steps, steps[1])] == ["s2", "s3"]
    assert [s["id"] for s in loop._collect_wave(steps, steps[3])] == ["s4"]


def test_collect_wave_does_not_merge_non_consecutive_same_group():
    steps = [_step("A", id="s1", parallel="wave-1"),
             _step("B", id="s2"),   # different step in between
             _step("C", id="s3", parallel="wave-1")]
    assert [s["id"] for s in loop._collect_wave(steps, steps[0])] == ["s1"]
    assert [s["id"] for s in loop._collect_wave(steps, steps[2])] == ["s3"]


def test_two_parallel_steps_run_concurrently_and_both_merge(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 2
    cwds = {c["cwd"] for c in fake.calls}
    assert len(cwds) == 2  # each ran in its OWN worktree, never the same dir
    assert not any(c["cwd"] == str(tmp_path) for c in fake.calls)  # never the main tree

    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-be"]["status"] == "ok"
    assert fresh.step_results["s-fe"]["status"] == "ok"


def test_connectors_replicated_into_each_worktree_with_absolute_env_dir(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"harn": {"command": "python3", "args": ["-m", "harn", "mcp"],
                                 "env": {"HARN_ENV_DIR": "harn_env"}}}}))
    captured_cwds = []
    class CapturingAdapter(WritingAdapter):
        def run_turn(self, prompt, cwd, **kw):
            captured_cwds.append(Path(cwd))
            return super().run_turn(prompt, cwd, **kw)
    fake = CapturingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    for wt in captured_cwds:
        mcp_json = json.loads((wt / ".mcp.json").read_text())
        assert mcp_json["mcpServers"]["harn"]["env"]["HARN_ENV_DIR"] == str(env.resolve())


def test_wave_of_one_runs_the_normal_single_step_path(tmp_path, monkeypatch):
    """A `parallel` id with no consecutive sibling must NOT create a worktree
    — it's just a regular step, unchanged from Phase 1/2 behavior."""
    env, t = _project(tmp_path, [
        _step("Solo", id="s1", parallel="wave-lonely"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 1
    assert fake.calls[0]["cwd"] == str(tmp_path)  # ran in the MAIN tree, no worktree


def test_command_type_step_can_participate_in_a_wave(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Lint", id="s-lint", type="command", command="true", parallel="wave-1"),
        _step("Build", id="s-build", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-lint"]["status"] == "ok"
    assert fresh.step_results["s-build"]["status"] == "ok"
    assert len(fake.calls) == 1  # only the agent step called the adapter


def test_wave_can_mix_two_different_agent_providers(tmp_path, monkeypatch):
    """The core agent-agnostic guarantee: nothing in the wave/worktree/merge
    code branches on WHICH provider a step uses. Two steps, two distinct
    (faked) adapters registered under different names, same wave."""
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1", agent="agent-a"),
        _step("Frontend", id="s-fe", parallel="wave-1", agent="agent-b"),
    ])
    agent_a, agent_b = WritingAdapter(), WritingAdapter()
    agent_a.name, agent_b.name = "agent-a", "agent-b"
    # "fake" (the project's [harn] default agent, from _project's harn.toml)
    # must also resolve, since _pick_adapter(cfg) computes a default adapter
    # up front even though both steps here override it explicitly.
    registry = {"agent-a": agent_a, "agent-b": agent_b, "fake": agent_a}
    monkeypatch.setattr(loop, "get_adapter", lambda n: registry[n])
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(agent_a.calls) == 1 and len(agent_b.calls) == 1
    assert (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-be"]["status"] == "ok"
    assert fresh.step_results["s-fe"]["status"] == "ok"


def test_no_worktrees_or_refs_leaked_after_a_wave_runs(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    code, out, _ = gitutil._run(["worktree", "list", "--porcelain"], tmp_path)
    # only the main worktree should remain
    assert out.count("worktree ") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_parallel_waves.py -q`
Expected: FAIL (`_collect_wave` doesn't exist; `run()` has no wave branch).

- [ ] **Step 3: Implement `_collect_wave` and `_replicate_connectors` first (pure/isolated, easiest to get right), then `_run_parallel_wave`, then wire it into `run()`'s step-walk.** Read the CURRENT `run()` body around the `if pending:` / `step = pending[0]` / command-type branch (roughly the block shown in this plan's Global Constraints research, but confirm exact current line numbers) before editing. Insert wave detection BEFORE the command-type check:

```python
if pending:
    step = pending[0]
    sid = step.get("id") or ""
    title = step.get("title", "")

    wave = _collect_wave(steps, step)
    if len(wave) >= 2:
        pending_wave = [s for s in wave if not _step_done(s.get("id"))]
        if pending_wave:
            outcome = _run_parallel_wave(env_dir, project_root, task, pending_wave,
                                         cfg, adapter)
            if outcome == "blocked" or outcome == "conflict_unresolved":
                return _run_end(env_dir, st)
            for s in pending_wave:
                done_ids.add(s.get("id") or "")
        continue

    if step.get("type") == "command":
        ... # existing, unchanged
```

`_run_parallel_wave`'s internals (implement exactly, this is the core of the task):

```python
def _run_parallel_wave(env_dir, project_root, task, wave, cfg, default_adapter) -> str:
    import concurrent.futures, tempfile, shutil
    wave_id = wave[0].get("parallel") or "wave"
    base_ref = gitutil.checkpoint(project_root, task.id, f"{wave_id}-base")
    tmp_root = Path(tempfile.mkdtemp(prefix=f"harn-wave-{wave_id}-"))
    worktrees = {}
    try:
        for step in wave:
            sid = step.get("id") or ""
            wt = tmp_root / sid
            if gitutil.create_worktree(project_root, base_ref, wt):
                _replicate_connectors(project_root, wt, env_dir)
                worktrees[sid] = wt

        results = {}
        def _run_one(step):
            sid = step.get("id") or ""
            wt = worktrees.get(sid)
            if wt is None:
                return sid, None  # worktree creation failed — treat as no-op patch
            if step.get("type") == "command":
                res = run_feedback(step.get("command", ""), wt)
                ok = res.ok
            else:
                step_adapter = _adapter_for_step(cfg, step, default_adapter)
                prompt = _build_step_prompt(env_dir, cfg, task, step,
                                           parallel_note=_PARALLEL_NOTE)
                result = _run_turn(step_adapter, env_dir, prompt, wt,
                                  task_id=task.id, stage=sid, step_title=step.get("title", ""),
                                  overrides=_step_overrides(cfg, step),
                                  tok_totals={}, tok_costs={}, cfg=cfg)
                ok = result.ok
            patch = gitutil.diff_as_patch(wt, base_ref)
            return sid, (ok, patch)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as ex:
            for sid, outcome in ex.map(_run_one, wave):
                results[sid] = outcome

        for sid, wt in worktrees.items():
            gitutil.remove_worktree(project_root, wt)

        blocked = (task.step_results.get("__wave_blocked__") or [])  # see block-handling note below
        return _merge_wave_patches(env_dir, project_root, task, wave, results, base_ref)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
```

(The exact block-detection mechanism — how a worktree agent's `BLOCKED.md` write is noticed by the coordinator thread — needs you to check how `_handle_block` currently detects a block for the MAIN tree today, i.e. does it read `env_dir/state/BLOCKED.md`, and does that path make sense when the agent ran with `cwd=worktree` but `env_dir` is still the ABSOLUTE main one? Since `_build_step_prompt`/`_run_turn` are called with the real `env_dir` (not a worktree copy) throughout, `ask_user`/`BLOCKED.md` writes go to the MAIN `env_dir/state/`, which is correct and requires no special handling — confirm this by reading `_handle_block`'s current signature before writing the merge/block logic in Task 5. This task (Task 4) can stop at "patches captured, ready to merge" — Task 5 owns the merge/block/conflict logic; do not overbuild it here if the interface above already cleanly separates them.)

`_PARALLEL_NOTE` constant (module level, near `_AUTO_NOTE`):
```python
_PARALLEL_NOTE = (
    "## You are one of several PARALLEL steps running right now\n"
    "Other steps in this wave are running CONCURRENTLY in their own isolated "
    "copies of the repo — you cannot see their in-progress changes, and they "
    "cannot see yours, until this wave finishes and merges. Avoid `ask_user` "
    "unless truly blocked: decide autonomously using current best practices "
    "and record your assumption via `record_decision` so it can be reviewed."
)
```

`_replicate_connectors` implementation:
```python
def _replicate_connectors(project_root: Path, worktree_path: Path, env_dir: Path) -> None:
    for rel in (".mcp.json", ".cursor/mcp.json"):
        src = project_root / rel
        if not src.exists():
            continue
        dst = worktree_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            cfg_json = json.loads(src.read_text())
            harn_server = cfg_json.get("mcpServers", {}).get("harn")
            if harn_server is not None:
                harn_server.setdefault("env", {})["HARN_ENV_DIR"] = str(env_dir.resolve())
            dst.write_text(json.dumps(cfg_json, indent=2))
        except (ValueError, OSError):
            shutil.copy(src, dst)  # best-effort: copy verbatim if we can't parse it
    for rel in ("AGENTS.md", "CLAUDE.md"):
        src = project_root / rel
        if src.exists():
            shutil.copy(src, worktree_path / rel)
```
(Add `import json` and `import shutil` to `loop.py`'s top-level imports if not already present — check first.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_parallel_waves.py -q`
Expected: most pass; the merge-dependent assertions (`step_results[...]["status"] == "ok"`) may need Task 5's merge function to exist — if `_merge_wave_patches` isn't implemented yet, stub it in THIS task to just `apply_patch` every clean patch in order and return `"advance"` (no conflict handling yet — that's Task 5's job) so this task's own tests can pass end-to-end. Note the stub clearly in your report so Task 5's implementer knows to replace it, not assume it's done.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q 2>&1 | tail -10`
Expected: fully green — the new wave branch is only reachable when a step's `parallel` field groups with a consecutive sibling, which no existing test's plan does.

- [ ] **Step 6: Bump version + commit**

```bash
git add harn/loop.py tests/test_parallel_waves.py harn/__init__.py pyproject.toml
git commit -m "feat(loop): parallel wave detection + worktree isolation + concurrent execution (Phase 3); version 0.18.0"
```

---

### Task 5: `loop.py` — patch merge, conflict → agent-merge turn, blocking

**Files:**
- Modify: `harn/loop.py`
- Test: `tests/test_parallel_waves.py` (extend)

**Interfaces:**
- Consumes: Task 4's `_run_parallel_wave` (replaces its stub `_merge_wave_patches` call), Task 2's `gitutil.apply_patch`/`save_patch_ref`, existing `_handle_block`, `_pick_adapter`.
- Produces: `_merge_wave_patches(env_dir, project_root, task, wave, results: dict, base_ref: str) -> str` — applies each wave member's captured patch to `project_root` (the MAIN tree) in `wave` order:
  1. `ok=False` member (agent turn failed, or command failed) → still attempt to apply its patch if non-empty (partial progress is still progress — record the step as `"failed"` in the ledger either way, matching single-step semantics) — but do NOT block the wave over an ordinary step failure; only an explicit `BLOCKED.md` write blocks.
  2. Clean `apply_patch` → ledger `step_results[sid] = {"status": "ok" if ok else "failed", ...}`, save the patch via `gitutil.save_patch_ref` (needed later for independent rollback — Task 6).
  3. Conflicting `apply_patch` (returns `False` and the patch was non-empty) → dispatch ONE agent-merge turn: build a prompt naming each wave member's title/body and its patch, ask the merge agent to resolve conflicts directly in the main tree (it runs a normal turn with `cwd=project_root`, since the conflict must be resolved in the ONE tree that matters). Use `_pick_adapter(cfg)` for the merge agent (no per-conflict `Agent:` override in Phase 3 — it's the run's default). If the merge turn's own `_handle_block` reports the agent called `ask_user`, return `"blocked"`.
  4. If any member wrote `BLOCKED.md` during its OWN turn (checked via the SAME `_handle_block(env_dir, cfg, st, state_dir, task, auto=False)` call already used elsewhere, since `BLOCKED.md` lives in the shared `env_dir/state/`, not per-worktree) — that member's patch is NOT applied, its ledger entry is `"blocked"`, its siblings still merge normally, and the wave overall returns `"blocked"` (matching Task 4's caller contract).

- [ ] **Step 1: Extend `tests/test_parallel_waves.py`** with:

```python
def test_disjoint_file_patches_both_merge_without_conflict(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()  # writes output_<dirname>.txt — disjoint by construction
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()


def test_conflicting_patches_dispatch_one_merge_agent_turn(tmp_path, monkeypatch):
    (tmp_path / "shared.txt").write_text("original\n")
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    class ConflictingAdapter:
        name = "fake"
        def __init__(self): self.calls = []
        def available(self): return True
        def run_turn(self, prompt, cwd, **kw):
            self.calls.append({"prompt": prompt, "cwd": str(cwd)})
            n = len(self.calls)
            if n <= 2:
                # both parallel steps edit the SAME line incompatibly
                Path(cwd, "shared.txt").write_text(f"edited by call {n}\n")
                return AgentResult(ok=True, text="done")
            # third call = the merge agent turn
            Path(cwd, "shared.txt").write_text("merged resolution\n")
            return AgentResult(ok=True, text="resolved the conflict")
    fake = ConflictingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 3  # 2 parallel + 1 merge turn
    assert (tmp_path / "shared.txt").read_text() == "merged resolution\n"


def test_parallel_step_block_defers_to_sequential_rerun(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    state_dir = env / "state"
    class BlockingAdapter:
        name = "fake"
        def __init__(self): self.calls = 0
        def available(self): return True
        def run_turn(self, prompt, cwd, **kw):
            self.calls += 1
            if "Backend" in prompt:
                (state_dir).mkdir(parents=True, exist_ok=True)
                (state_dir / "BLOCKED.md").write_text("## Question\nWhich DB?\n")
            else:
                Path(cwd, "frontend_done.txt").write_text("ok\n")
            return AgentResult(ok=True, text="...")
    fake = BlockingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    phase = loop.run(tmp_path, env)

    from harn import state as state_mod
    assert phase == state_mod.BLOCKED
    # Frontend's patch merged despite Backend blocking
    assert (tmp_path / "frontend_done.txt").exists()
    fresh = tasks.find(env, t.id)
    assert fresh.step_results.get("s-fe", {}).get("status") == "ok"
```

(Check `AgentResult` import is present at the top of the test file — add `from harn.adapters.base import AgentResult` if the extended tests need it beyond what Task 4 already imported.)

- [ ] **Step 2: Run tests to verify they fail** (or pass trivially against Task 4's stub in a way that doesn't actually prove conflict/block handling — read the stub's behavior first to confirm these tests genuinely exercise NEW logic).

Run: `python3 -m pytest tests/test_parallel_waves.py -q -k "conflict or block or disjoint"`

- [ ] **Step 3: Replace Task 4's `_merge_wave_patches` stub with the real implementation** per the Interfaces section. Read `_handle_block`'s current signature and the existing oracle/reconcile agent-dispatch pattern (`_run_onfail_handler` from Phase 2 is the closest precedent for "dispatch one extra agent turn mid-engine, handle its own block") before writing the merge-agent dispatch, and follow the same shape.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_parallel_waves.py -q`
Expected: all pass.

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q 2>&1 | tail -10`
Expected: fully green.

- [ ] **Step 6: Bump version + commit**

```bash
git add harn/loop.py tests/test_parallel_waves.py harn/__init__.py pyproject.toml
git commit -m "feat(loop): wave patch merge, agent-merge-turn conflict resolution, block handling (Phase 3); version 0.18.1"
```

---

### Task 6: Independent rollback + `run_step()`/studio Rerun for one parallel step

**Files:**
- Modify: `harn/loop.py`
- Test: `tests/test_parallel_waves.py` (extend), `tests/test_run_step.py` (extend)

**Interfaces:**
- Consumes: Task 2's `gitutil.apply_patch(reverse=True)`, `load_patch_ref`; existing `gitutil.rollback_to`.
- Produces: `rollback_parallel_step(project_root: Path, env_dir: Path, task_id: str, step_id: str) -> dict` — loads the step's saved patch via `gitutil.load_patch_ref`, attempts `gitutil.apply_patch(project_root, patch, reverse=True)`; on success returns `{"ok": True, "mode": "single-step"}`; on failure, falls back to `gitutil.rollback_to(base_ref, project_root, apply=True)` (`base_ref` read from the step's own `stage_checkpoints` entry — reuse the EXISTING per-step checkpoint mechanism from Phase 1/2, which already stores a ref per step id) and returns `{"ok": bool, "mode": "whole-wave-fallback", "note": "single-step rollback was no longer possible; reverted the whole wave to its starting point"}` — the caller (studio UI) must display this note so the fallback is never silent.

- [ ] **Step 1: Write the failing tests.** Extend `tests/test_parallel_waves.py`:

```python
def test_rollback_one_parallel_step_leaves_sibling_untouched(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()

    r = loop.rollback_parallel_step(tmp_path, env, t.id, "s-be")
    assert r["ok"] is True and r["mode"] == "single-step"
    assert not (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()  # sibling untouched


def test_rollback_falls_back_to_whole_wave_when_patch_no_longer_reverses(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    # someone edits the SAME lines the step's patch touches, after the merge
    (tmp_path / "output_s-be.txt").write_text("edited again after merge, incompatible\n")

    r = loop.rollback_parallel_step(tmp_path, env, t.id, "s-be")
    assert r["mode"] == "whole-wave-fallback"
    assert "note" in r and r["note"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_parallel_waves.py -q -k rollback`

- [ ] **Step 3: Implement `rollback_parallel_step`** per the Interfaces section. Confirm the per-step checkpoint written during wave setup (Task 4) is keyed the same way `_checkpoint_stage`/`task.stage_checkpoints` already works elsewhere, so this function can find `base_ref` the same way `run_step`'s existing rerun logic does.

- [ ] **Step 4: Wire studio's existing per-step Rerun button to call this for a parallel-grouped step.** Read `harn/studio.py`'s current `launch_step`/`run_step` studio-backend wiring (from Phase 1/2) and add a route or branch: if the step being rerun has a non-empty `parallel` field, call `rollback_parallel_step` first (studio-side), surfacing the returned `note` in the UI (an alert or inline message) if `mode == "whole-wave-fallback"`, THEN proceed with the normal rerun-this-step flow.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_parallel_waves.py tests/test_run_step.py -q`

- [ ] **Step 6: Run the full suite + JS syntax check**

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

- [ ] **Step 7: Bump version + commit**

```bash
git add harn/loop.py harn/studio.py tests/test_parallel_waves.py tests/test_run_step.py harn/__init__.py pyproject.toml
git commit -m "feat: independent rollback for one parallel step, wired into studio Rerun (Phase 3); version 0.18.2"
```

---

### Task 7: Studio UI — gesture → field bridge + the "lane" visual

**Files:**
- Modify: `harn/studio.py`
- Test: live browser verification via Claude Preview MCP (no new Python tests — pure frontend).

**Interfaces:**
- `deriveParallelGroups()` — on drag-end (find the existing drag-end handler from Phase 1's canvas work), bucket enabled step nodes into horizontal bands: nodes within a Y-threshold (`±30px`, tune during live testing) of each other AND contiguous in the current step order get a shared `parallel` group id (reuse an existing member's id if any already has one; else mint `wave-<6hex>` — reuse the SAME id-generation approach as `workflow.ensure_ids` for consistency, or a simple `Math.random().toString(16)` client-side since server-side `ensure_ids` only handles step `id`, not `parallel` group ids). A band that ends up with exactly one member gets `parallel: ''`.
- Lane rendering in `renderFlow()`: for each non-empty `parallel` group with ≥2 members, draw a backdrop `<div class="lane">` positioned to span the bounding box of its members (behind them, `pointer-events:none`), with a header chip `∥ parallel · <group>`. Edges: the existing edge-drawing code (SVG lines between sequential nodes) needs to fan out from the pre-wave step to every member and re-converge from every member to the post-wave step — find the current edge-computation function and extend it for wave membership rather than a strict linear chain.
- Inspector: for a step with non-empty `parallel`, add a note "Part of parallel group `<group>` — runs concurrently with N other step(s)" and a "Make sequential" button (`onclick` clears `parallel` on every member of that group, then re-renders).

- [ ] **Step 1: Read the current drag-end handler, `renderFlow()`'s node/edge rendering, and `renderInsp()`'s structure** (grep for the drag-related function names and the SVG edge-drawing code) before writing anything — Phases 1/2 already established these; match their exact style/CSS variable conventions (`var(--accent)` etc.) rather than inventing new ones.

- [ ] **Step 2: Implement `deriveParallelGroups()`, called from the drag-end handler** (and also from a toolbar action if a manual "detect parallel groups" trigger is more robust than relying purely on drag-end — use your judgment once you see the current drag code; note your choice in the report).

- [ ] **Step 3: Implement the lane backdrop + fan-out/converge edges in `renderFlow()`.**

- [ ] **Step 4: Implement the inspector note + "Make sequential" button in `renderInsp()`'s step-type-agnostic section** (it applies regardless of whether the step is `Type: agent` or `Type: command`).

- [ ] **Step 5: JS syntax check**

```bash
python3 -c "
import re
text = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', text, re.S)
open('/tmp/studio_check.js','w').write(m.group(1))
"
node --check /tmp/studio_check.js
```

- [ ] **Step 6: Full pytest** — `python3 -m pytest -q` → green (pure frontend task, no Python behavior change expected).

- [ ] **Step 7: Live browser verification (not optional).** Scratch project in `/tmp`, `python3 -m harn.cli setup <path> --no-install --no-onboard`, temp `.claude/launch.json` running `python3 -m harn.cli ui <path> --port <unused-port> --no-open`, drive via Claude Preview MCP tools. Verify:
  1. Dragging two step nodes to the same Y-band assigns them a shared `parallel` group id (inspect `S.workflow.nodes` via `preview_eval`) and a lane backdrop appears behind them with the `∥ parallel · wave-...` chip.
  2. Dragging one of them away (different Y) clears its `parallel` field and the lane disappears/shrinks to reflect the remaining member(s).
  3. The inspector's "Make sequential" button clears `parallel` on both members.
  4. Saving (`saveFlow()`) then re-parsing the saved WORKFLOW.md server-side shows matching `Parallel: <group>` lines on both steps.
  Clean up: stop the preview server, remove the scratch project and temp launch.json. **Also confirm no `harn watch` process was left running** (`ps aux | grep "harn watch"` before and after) — per the standing lesson from this session, kill anything you find and do not treat it as someone else's problem.

- [ ] **Step 8: Bump version + commit**

```bash
git add harn/studio.py harn/__init__.py pyproject.toml
git commit -m "feat(studio): drag-to-parallel gesture, lane visual, Make-sequential button (Phase 3); version 0.19.0"
```

---

### Task 8: Documentation + config-error test for `On fail:` inside a wave

**Files:**
- Modify: `README.md` (only if it documents per-step fields — check first, per the established Phase 1/2 precedent)
- Test: `tests/test_parallel_waves.py` (extend, one more test)

**Interfaces:** none new.

- [ ] **Step 1: Add the config-error test.** A parallel-grouped step with `On fail:` set must log `config_error` and behave as if unset (per the spec's non-goal). Extend `tests/test_parallel_waves.py`:

```python
def test_onfail_inside_a_wave_is_a_config_error(tmp_path, monkeypatch):
    from harn import events
    env, t = _project(tmp_path, [
        _step("Tests", id="s-t", type="command", command="false",
              parallel="wave-1", on_fail="s-fix"),
        _step("Build", id="s-build", parallel="wave-1"),
        _step("Fix", id="s-fix"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    evs = events.read(env)
    errs = [e for e in evs if e["event"] == "config_error" and e.get("stage") == "s-t"]
    assert errs, "expected a config_error for on_fail set inside a parallel wave"
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-t"]["status"] == "failed"  # recorded, not retried
```

Implement the check inside `_run_parallel_wave`/`_run_one` (Task 4/5's code): before running a command-type wave member, if `step.get("on_fail")` is set, emit `events.emit(env_dir, "config_error", task_id=task.id, stage=sid, detail="On fail is not supported inside a parallel wave (Phase 3 non-goal)")` and treat `on_fail` as unset for that member's execution (don't dispatch a handler even on failure).

- [ ] **Step 2: Run it**

Run: `python3 -m pytest tests/test_parallel_waves.py -q -k onfail`

- [ ] **Step 3: Check for a Phase-1/2-style README reference**

```bash
grep -n "Agent:\|Type:\|Command:\|On fail:\|per-step" README.md
```

If found, add one or two lines documenting `Parallel: <group>` in the same terse style. If nothing found (matching the precedent that README never got per-step field docs), report that and skip — no new section.

- [ ] **Step 4: Full suite**

Run: `python3 -m pytest -q 2>&1 | tail -5`

- [ ] **Step 5: Bump version + commit**

```bash
git add tests/test_parallel_waves.py README.md harn/__init__.py pyproject.toml
git commit -m "test: on_fail inside a parallel wave is a config error, not silently retried; version 0.19.1"
```
