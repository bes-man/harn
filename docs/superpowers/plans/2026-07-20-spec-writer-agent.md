# spec-writer Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a default `spec-writer` role + workflow preset that every new harn project gets out of the box, turning a vague task into a locked, software-spec-shaped set of acceptance criteria via research/competitor/best-practice/risk analysis and Telegram-routed clarifying questions.

**Architecture:** Two static content files under `harn/templates/` (an `agents/spec-writer.md` role definition and a `workflows/spec-writer.json` 7-step workflow preset), copied into every project's `harn_env/` by the existing `scaffold.setup()` tree-copy — no changes to any `.py` module. Correctness is verified by round-tripping the templates through `scaffold.setup()` into a throwaway project and asserting `roles.discover()` / `workflows.load()` parse them exactly as intended.

**Tech Stack:** Python stdlib only (`pytest` for tests), YAML-ish frontmatter (harn's own lightweight parser in `roles.py`), plain JSON for the workflow preset — matches every other role/workflow in the repo.

## Global Constraints

- Every committed `harn/` change bumps `__version__` in `harn/__init__.py` and the version in `pyproject.toml` (project convention — proves the change actually shipped).
- No new MCP tools, no changes to `roles.py`/`workflows.py`/`scaffold.py` — this is pure template content (per design doc scope).
- Role frontmatter fields: `name: spec-writer`, `command: spec`, `status: todo`, `trigger: manual`, `workflow: spec-writer`, `oracle: false`, `isolation: main`, `push: false` (no `next_status` line — default `""` means no transition).
- Workflow preset must have exactly 7 `kind: step` nodes with non-empty `title`/`body`, in this order: Research, Competitor analysis, Best practices, Risk analysis, Questions to user, Draft spec, Lock spec.
- Task `Description` structure written by step 6 uses these exact headings, in order: `## Problem`, `## Goal`, `## Scope`, `## Requirements`, `## Constraints & risks`, `## Acceptance criteria`.

---

### Task 1: `spec-writer` role template

**Files:**
- Create: `harn/templates/agents/spec-writer.md`
- Test: `tests/test_spec_writer_agent.py`

**Interfaces:**
- Consumes: `harn.scaffold.setup(project_root)` (existing, copies `harn/templates/` → `harn_env/`), `harn.roles.discover(env_dir) -> list[Role]` (existing).
- Produces: nothing consumed by later tasks in-process — Task 2 is a sibling file, not a dependency. Both tasks share the same test file (appended to, not recreated).

- [ ] **Step 1: Write the failing test**

Create `tests/test_spec_writer_agent.py`:

```python
"""spec-writer default role + workflow preset (docs/superpowers/specs/
2026-07-20-spec-writer-agent-design.md): ships via harn/templates/ so every
new project gets it, discoverable/loadable exactly like a hand-authored
role/workflow."""
from __future__ import annotations

import subprocess

from harn import ENV_DIRNAME, roles, scaffold, workflows


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _scaffolded_env(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    return tmp_path / ENV_DIRNAME


def test_spec_writer_role_discovered(tmp_path):
    env = _scaffolded_env(tmp_path)
    found = [r for r in roles.discover(env) if r.name == "spec-writer"]
    assert len(found) == 1, "spec-writer role not shipped/discovered"
    r = found[0]
    assert r.command == "spec"
    assert r.status == "todo"
    assert r.trigger == "manual"
    assert r.next_status == ""
    assert r.workflow == "spec-writer"
    assert r.oracle is False
    assert r.isolation == "main"
    assert r.push is False
    assert r.body().strip(), "role must have a persona body"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_spec_writer_agent.py::test_spec_writer_role_discovered -v`
Expected: FAIL — `assert len(found) == 1` fails with `0 == 1` (no `spec-writer.md` shipped yet).

- [ ] **Step 3: Write the role template**

Create `harn/templates/agents/spec-writer.md`:

```markdown
---
name: spec-writer
command: spec
status: todo
trigger: manual
workflow: spec-writer
oracle: false
isolation: main
push: false
---

## Role: spec-writer

You turn a vague task into a spec worth building from. You do not accept the
first framing of a problem — you research it, you check how others have
solved it, you name the risks nobody mentioned yet, and you ask the human
only the questions you genuinely cannot resolve yourself.

Ground rules:
- Competitor research and best-practice research are INPUTS to your
  judgment, never headings in the final spec. The human reads a spec, not
  your research log.
- Every claim in the final `## Requirements` or `## Constraints & risks`
  section should trace back to something you actually found in this task's
  research steps (findings live in the task's `## Context`, appended
  automatically) — never invent a requirement to sound thorough.
- Ask ONE question at a time via `ask_user(question, skill=...)`, most
  important first. Never batch multiple unresolved questions into one
  message, and never guess past a genuine ambiguity — stop and ask.
- `## Acceptance criteria` must be one observable, independently verifiable
  fact per line — "the API returns 404 for an unknown id", not "handles
  errors gracefully".
- You never write code and never change `status`/`next_status` yourself —
  your only job is to leave the task with a spec worth implementing against,
  then call `lock_spec` to close the funnel.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_spec_writer_agent.py::test_spec_writer_role_discovered -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add harn/templates/agents/spec-writer.md tests/test_spec_writer_agent.py
git commit -m "feat: ship spec-writer default role template"
```

---

### Task 2: `spec-writer` workflow preset

**Files:**
- Create: `harn/templates/workflows/spec-writer.json`
- Modify: `tests/test_spec_writer_agent.py` (append a second test)

**Interfaces:**
- Consumes: `harn.workflows.load(env_dir, name) -> dict | None` (existing) — returns `{"name", "title", "description", "version", "preamble", "nodes"}` per `workflows._norm`.
- Produces: a `harn_env/workflows/spec-writer.json` file whose `name` field (`"spec-writer"`) matches the role's `workflow:` value from Task 1 — this is the linkage `roles_runner.run_role` relies on (`workflow_name or task.workflow` → `workflows_mod.snapshot_for_task`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_spec_writer_agent.py`:

```python
_EXPECTED_STEP_TITLES = [
    "Research",
    "Competitor analysis",
    "Best practices",
    "Risk analysis",
    "Questions to user",
    "Draft spec",
    "Lock spec",
]


def test_spec_writer_workflow_preset_loads(tmp_path):
    env = _scaffolded_env(tmp_path)
    wf = workflows.load(env, "spec-writer")
    assert wf is not None, "spec-writer.json not shipped/loadable"
    assert wf["name"] == "spec-writer"
    steps = [n for n in wf["nodes"] if n.get("kind") == "step"]
    assert [s["title"] for s in steps] == _EXPECTED_STEP_TITLES
    for s in steps:
        assert s["id"], f"step {s['title']!r} missing id"
        assert s["body"].strip(), f"step {s['title']!r} missing body"
    ids = [s["id"] for s in steps]
    assert len(ids) == len(set(ids)), "step ids must be unique"


def test_spec_writer_role_workflow_matches_preset_name(tmp_path):
    env = _scaffolded_env(tmp_path)
    role = next(r for r in roles.discover(env) if r.name == "spec-writer")
    wf = workflows.load(env, role.workflow)
    assert wf is not None, "role's workflow: value must resolve to a real preset"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_spec_writer_agent.py -v`
Expected: `test_spec_writer_role_discovered` PASSes (from Task 1); the two new
tests FAIL — `wf is not None` fails because `spec-writer.json` doesn't exist
yet.

- [ ] **Step 3: Write the workflow preset**

Create `harn/templates/workflows/spec-writer.json`:

```json
{
  "name": "spec-writer",
  "title": "Spec Writer",
  "description": "Research a task, ask the human what's genuinely ambiguous, and lock a software-spec-shaped set of acceptance criteria.",
  "version": "1",
  "preamble": "",
  "nodes": [
    {
      "kind": "step",
      "id": "step-research",
      "title": "Research",
      "body": "Read the task, any linked PRD (`read_prd`), and relevant code/skills (`project`, `architecture`). Restate the problem as one AS-IS paragraph and one TO-BE paragraph. This framing is what every later step reasons against — get it right before moving on.",
      "required": ["project", "architecture"],
      "tools": ["read_prd"],
      "agent": "", "model": "", "effort": "", "temperature": "",
      "enabled": true
    },
    {
      "kind": "step",
      "id": "step-competitor-analysis",
      "title": "Competitor analysis",
      "body": "Research how similar products or features solve this same problem (web search / context7 where the CLI adapter supports it). If live web search isn't available, reason from training knowledge and explicitly flag the gap rather than guessing silently. Record findings as private notes for the draft — competitor names/approaches are NOT a section in the final spec.",
      "required": [],
      "tools": [],
      "agent": "", "model": "", "effort": "", "temperature": "",
      "enabled": true
    },
    {
      "kind": "step",
      "id": "step-best-practices",
      "title": "Best practices",
      "body": "Identify current technical best practices and patterns for this kind of change (skills: `standards`, `architecture`; context7 for library/API specifics). Same rule as competitor analysis: these findings become concrete `## Requirements` lines later, never their own heading.",
      "required": ["standards", "architecture"],
      "tools": [],
      "agent": "", "model": "", "effort": "", "temperature": "",
      "enabled": true
    },
    {
      "kind": "step",
      "id": "step-risk-analysis",
      "title": "Risk analysis",
      "body": "List technical, security, and edge-case risks (skills: `security`, `constraints`). Pair every risk with either a mitigation or an open question for the next step — a risk with neither is not finished.",
      "required": ["security", "constraints"],
      "tools": [],
      "agent": "", "model": "", "effort": "", "temperature": "",
      "enabled": true
    },
    {
      "kind": "step",
      "id": "step-questions",
      "title": "Questions to user",
      "body": "For every ambiguity still open after research/best-practices/risk analysis, call `ask_user(question, skill=...)` — highest-leverage question first, one at a time. Answers arrive via chat or Telegram automatically; do not proceed past a genuine ambiguity by guessing.",
      "required": [],
      "tools": ["ask_user", "check_pending_answer", "answer_question"],
      "agent": "", "model": "", "effort": "", "temperature": "",
      "enabled": true
    },
    {
      "kind": "step",
      "id": "step-draft-spec",
      "title": "Draft spec",
      "body": "Write the task's Description via `update_task(task_id, description=...)` using exactly these headings, in this order: `## Problem`, `## Goal`, `## Scope`, `## Requirements`, `## Constraints & risks`, `## Acceptance criteria`. `## Acceptance criteria` is one observable, independently verifiable fact per line. If the task has `prds` set, also update the linked `harn_env/prd/<slug>.md` directly (edit the file, keep its existing Problem/Goal/Scope/Acceptance criteria/Open questions structure) so the global requirement isn't stranded in one task.",
      "required": [],
      "tools": ["update_task"],
      "agent": "", "model": "", "effort": "", "temperature": "",
      "enabled": true
    },
    {
      "kind": "step",
      "id": "step-lock-spec",
      "title": "Lock spec",
      "body": "Call `lock_spec(task_id, done_when=..., approach=..., decisions=[...])` with the same acceptance criteria written in the Draft spec step, closing the clarification funnel. After this, the task is ready for the normal implementer pipeline to pick up via `get_next_task`.",
      "required": [],
      "tools": ["lock_spec"],
      "agent": "", "model": "", "effort": "", "temperature": "",
      "enabled": true
    }
  ]
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_spec_writer_agent.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add harn/templates/workflows/spec-writer.json tests/test_spec_writer_agent.py
git commit -m "feat: ship spec-writer workflow preset"
```

---

### Task 3: Version bump + full suite check

**Files:**
- Modify: `harn/__init__.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing consumed by other tasks — this is the closing task.

- [ ] **Step 1: Bump the version**

Read current version first:

```bash
grep -n "__version__" harn/__init__.py
grep -n "^version" pyproject.toml
```

Both currently read `0.17.114`. Bump the patch component to `0.17.115` in
both files:

`harn/__init__.py`:
```python
__version__ = "0.17.115"
```

`pyproject.toml`:
```toml
version = "0.17.115"
```

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass, including the 3 new ones in
`tests/test_spec_writer_agent.py` and the pre-existing `test_agent_roles.py`
/ `test_workflows_presets.py` suites (unaffected by this change, confirming
no regression).

- [ ] **Step 3: Commit**

```bash
git add harn/__init__.py pyproject.toml
git commit -m "chore: bump version to 0.17.115"
```
