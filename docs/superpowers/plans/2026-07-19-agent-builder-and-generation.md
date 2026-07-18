# Agent Builder + Generation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A visual Agents tab in Studio to create/edit roles, and a generate-from-prompt that drafts a role's flow and selects from existing skills/tools, human-confirmed before save.

**Architecture:** Backend first — `roles.save`/`roles.delete` (the write half of the existing discovery-only `roles.py`), then `agentgen.generate` (one catalog-grounded LLM turn returning a validated draft, never writing), then Studio payloads/routes, then the Agents tab UI in the single `_HTML` string, then browser verification. Each backend piece is unit-tested against real round-trips; the generator is tested with a stubbed adapter.

**Tech Stack:** Python stdlib, existing harn conventions (frontmatter round-trip like skills/tools, `http.server` Studio, vanilla-JS `_HTML` — no new dependency).

## Global Constraints

- No new third-party dependency.
- The generator NEVER writes files and NEVER invents a skill/tool — it only selects from `skills.discover`/`tools.discover`; unknown names are dropped and reported. Files are written only on an explicit human Save.
- `roles.save` must round-trip exactly through the existing `roles.discover` (same frontmatter keys/shapes) — a saved agent must re-read identically.
- No-config / no-agents projects behave exactly as today.
- Follow `docs/superpowers/specs/2026-07-19-agent-builder-and-generation-design.md`.

---

### Task 1: `roles.save` + `roles.delete` (write half of roles.py)

**Files:**
- Modify: `harn/roles.py`
- Test: `tests/test_agent_builder.py` (new)

**Interfaces:**
- Produces: `roles.save(env_dir, data: dict) -> Path` writes `harn_env/agents/<name>.md` (frontmatter from the role fields + a `## Role` body from `data["body"]`), creating `agents/` if absent; `roles.delete(env_dir, name) -> bool`. A dict saved then `roles.discover`'d yields a `Role` with identical field values.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_builder.py
"""Agent builder + generation spec (docs/superpowers/specs/2026-07-19-agent-builder-and-generation-design.md)."""
from __future__ import annotations
from harn import roles, ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    return env


def test_save_then_discover_round_trips(tmp_path):
    env = _env(tmp_path)
    p = roles.save(env, {
        "name": "analyst", "command": "an", "status": "analyzing",
        "next_status": "analyzed", "trigger": "manual", "oracle": False,
        "isolation": "worktree", "secrets": ["SSH_HOST"], "agent": "claude",
        "model": "sonnet", "workflow": "analyst-flow",
        "body": "## Role\nYou are the analyst.",
    })
    assert p.name == "analyst.md"
    r = roles.find(env, "analyst")
    assert r.name == "analyst" and r.command == "an" and r.status == "analyzing"
    assert r.next_status == "analyzed" and r.oracle is False
    assert r.isolation == "worktree" and r.secrets == ["SSH_HOST"]
    assert r.agent == "claude" and r.model == "sonnet" and r.workflow == "analyst-flow"
    assert "You are the analyst." in r.body()


def test_save_creates_agents_dir_if_missing(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    roles.save(env, {"name": "dev", "status": "todo", "body": "x"})
    assert (env / "agents" / "dev.md").exists()


def test_delete_removes_role(tmp_path):
    env = _env(tmp_path)
    roles.save(env, {"name": "dev", "status": "todo", "body": "x"})
    assert roles.delete(env, "dev") is True
    assert roles.find(env, "dev") is None
    assert roles.delete(env, "dev") is False   # already gone
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_builder.py -v`
Expected: FAIL — `module 'harn.roles' has no attribute 'save'`.

- [ ] **Step 3: Implement**

In `harn/roles.py`, add a filename-safe helper and the two functions:

```python
def _safe_name(name: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Za-z0-9._-]", "-", name.strip()) or "agent"


_ROLE_FM_FIELDS = ("name", "command", "status", "trigger", "next_status",
                   "workflow", "oracle", "secrets", "isolation", "agent", "model")


def _render_role(data: dict) -> str:
    def fmt(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, list):
            return "[" + ", ".join(str(x) for x in v) + "]"
        return str(v)
    lines = ["---"]
    for k in _ROLE_FM_FIELDS:
        if k in data and data[k] not in (None, ""):
            lines.append(f"{k}: {fmt(data[k])}")
    lines.append("---")
    body = (data.get("body") or "").strip()
    return "\n".join(lines) + "\n\n" + body + "\n"


def save(env_dir: Path, data: dict) -> Path:
    name = _safe_name(str(data.get("name") or ""))
    d = env_dir / "agents"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    path.write_text(_render_role({**data, "name": name}), encoding="utf-8")
    return path


def delete(env_dir: Path, name: str) -> bool:
    path = env_dir / "agents" / f"{_safe_name(name)}.md"
    if path.exists():
        path.unlink()
        return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_agent_builder.py -v`
Expected: 3 passed.

- [ ] **Step 5: Full suite**

Run: `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/roles.py tests/test_agent_builder.py
git commit -m "feat(roles): save/delete write half of the agents directory"
```

---

### Task 2: `agentgen.generate` (catalog-grounded generation turn)

**Files:**
- Create: `harn/agentgen.py`
- Test: `tests/test_agent_builder.py`

**Interfaces:**
- Consumes: `skills.discover`/`tools.discover`/`tasks.lifecycle`, `loop.get_adapter`.
- Produces: `agentgen.generate(env_dir, cfg, description) -> dict` returning `{"role": {...}, "workflow": {"nodes": [...]}, "dropped": [...]}`. Validates every skill/tool name in the model's draft against the live catalogs; unknown names go to `dropped` and are removed from the draft. Unknown status → the pipeline's first status. Never writes files; never raises (a bad model reply → a safe minimal draft).

- [ ] **Step 1: Write the failing tests**

```python
def test_generate_validates_against_catalogs(tmp_path, monkeypatch):
    from harn import agentgen, skills, tools
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    (env / "skills" / "standards").mkdir(parents=True)
    (env / "skills" / "standards" / "SKILL.md").write_text(
        "---\nname: standards\ndescription: d\n---\nbody", encoding="utf-8")

    import json as _json
    reply = _json.dumps({
        "role": {"name": "triager", "status": "todo", "next_status": "done",
                 "oracle": True, "body": "You triage."},
        "workflow": {"nodes": [
            {"kind": "step", "title": "Read", "required": ["standards", "ghost_skill"],
             "tools": ["nonexistent_tool"]}]},
    })

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None, temperature=None):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)

    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "an agent that triages bug reports")
    assert out["role"]["name"] == "triager"
    # ghost_skill / nonexistent_tool are not in the catalog → dropped
    step = out["workflow"]["nodes"][0]
    assert step["required"] == ["standards"]
    assert step["tools"] == []
    assert "ghost_skill" in out["dropped"] and "nonexistent_tool" in out["dropped"]


def test_generate_unknown_status_falls_back_to_first(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    import json as _json
    reply = _json.dumps({"role": {"name": "x", "status": "bogus", "body": "b"},
                         "workflow": {"nodes": []}})

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text=reply)
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    assert out["role"]["status"] == "todo"   # first of default lifecycle


def test_generate_malformed_reply_never_raises(tmp_path, monkeypatch):
    from harn import agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)

    class FakeAdapter:
        name = "fake"
        def available(self): return True
        def run_turn(self, *a, **k):
            from harn.adapters.base import AgentResult
            return AgentResult(ok=True, text="not json at all")
    monkeypatch.setattr(agentgen.loop_mod, "get_adapter", lambda n: FakeAdapter())
    out = agentgen.generate(env, Config(), "desc")
    assert isinstance(out, dict) and "role" in out and "workflow" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_builder.py -v -k generate`
Expected: FAIL — no module `harn.agentgen`.

- [ ] **Step 3: Implement**

Create `harn/agentgen.py`:

```python
"""Generate an agent (role) draft from a natural-language description.

One LLM turn, grounded in the project's EXISTING skills/tools/statuses. The
model selects capabilities from those catalogs; it never invents a skill or
tool, and this module never writes a file — the caller (Studio) persists only
on an explicit human Save. See
docs/superpowers/specs/2026-07-19-agent-builder-and-generation-design.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import loop as loop_mod
from . import skills as skills_mod
from . import tools as tools_mod
from . import tasks as tasks_mod


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    m = _JSON_RE.search(text or "")
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _build_prompt(env_dir: Path, description: str, statuses: list[str]) -> str:
    return (
        "You design an agent (a role) for the harn task system. Output ONLY a "
        "JSON object, no prose.\n\n"
        f"Description of the agent to build:\n{description}\n\n"
        f"Board statuses (pick `status` and `next_status` from these): {statuses}\n\n"
        "Available skills (choose `required` ONLY from these names):\n"
        + skills_mod.index(env_dir) + "\n\n"
        "Available tools (choose step `tools` ONLY from these names):\n"
        + tools_mod.index(env_dir) + "\n\n"
        'Shape: {"role": {"name","command","status","next_status","oracle",'
        '"isolation","body"}, "workflow": {"nodes": [{"kind":"step","title",'
        '"body","required":[skill names],"tools":[tool names]}]}}'
    )


def generate(env_dir: Path, cfg, description: str) -> dict:
    statuses = tasks_mod.lifecycle(env_dir)
    known_skills = {s.name for s in skills_mod.discover(env_dir)}
    known_tools = {t.name for t in tools_mod.discover(env_dir)}
    adapter = loop_mod.get_adapter(getattr(cfg, "agent", "") or "claude")
    prompt = _build_prompt(env_dir, description, statuses)
    dropped: list[str] = []
    try:
        res = adapter.run_turn(prompt, env_dir.parent,
                               **({"model": cfg.model} if getattr(cfg, "model", "") else {}))
        draft = _extract_json(res.text)
    except Exception:
        draft = {}

    role = dict(draft.get("role") or {})
    role.setdefault("name", "agent")
    if role.get("status") not in statuses:
        role["status"] = statuses[0] if statuses else "todo"
    if role.get("next_status") and role["next_status"] not in statuses:
        role["next_status"] = ""

    wf = draft.get("workflow") or {}
    nodes = wf.get("nodes") if isinstance(wf, dict) else None
    clean_nodes = []
    for n in (nodes or []):
        if not isinstance(n, dict):
            continue
        req = [s for s in (n.get("required") or []) if s in known_skills]
        dropped += [s for s in (n.get("required") or []) if s not in known_skills]
        tl = [t for t in (n.get("tools") or []) if t in known_tools]
        dropped += [t for t in (n.get("tools") or []) if t not in known_tools]
        clean_nodes.append({**n, "kind": "step", "required": req, "tools": tl})

    return {"role": role, "workflow": {"nodes": clean_nodes},
            "dropped": sorted(set(dropped))}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_agent_builder.py -v -k generate`
Expected: 3 passed.

- [ ] **Step 5: Full suite**

Run: `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/agentgen.py tests/test_agent_builder.py
git commit -m "feat(agentgen): generate an agent draft from NL, grounded in existing skills/tools"
```

---

### Task 3: Studio payloads + routes (list / save / delete / generate)

**Files:**
- Modify: `harn/studio.py`
- Test: `tests/test_agent_builder.py`

**Interfaces:**
- Consumes: `roles.discover`/`save`/`delete` (Task 1), `agentgen.generate` (Task 2).
- Produces: `agents_payload(env_dir) -> dict` (`{"agents": [...], "statuses": [...], "skills": [...], "tools": [...]}`), `save_agent_payload`, `delete_agent_payload`, `generate_agent_payload(env_dir, project_root, cfg, body)`; routes `GET /api/agents`, `POST /api/agents/save`, `POST /api/agents/delete`, `POST /api/agents/generate`. `generate_agent_payload` MUST NOT write any file.

- [ ] **Step 1: Write the failing tests**

```python
def test_agents_payload_lists_and_save_delete_roundtrip(tmp_path):
    from harn import studio
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    r = studio.save_agent_payload(env, {"name": "dev", "status": "todo", "body": "b"})
    assert r["ok"] is True
    payload = studio.agents_payload(env)
    assert any(a["name"] == "dev" for a in payload["agents"])
    assert "todo" in [s if isinstance(s, str) else s["name"] for s in payload["statuses"]]
    d = studio.delete_agent_payload(env, {"name": "dev"})
    assert d["ok"] is True
    assert not any(a["name"] == "dev" for a in studio.agents_payload(env)["agents"])


def test_generate_agent_payload_never_writes(tmp_path, monkeypatch):
    from harn import studio, agentgen
    from harn.config import Config
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    (env / "tasks").mkdir(parents=True)
    monkeypatch.setattr(agentgen, "generate",
                        lambda e, c, d: {"role": {"name": "x", "status": "todo"},
                                         "workflow": {"nodes": []}, "dropped": []})
    r = studio.generate_agent_payload(env, env.parent, Config(), {"description": "d"})
    assert r["ok"] is True and r["draft"]["role"]["name"] == "x"
    assert list((env / "agents").glob("*.md")) == []   # nothing persisted
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_agent_builder.py -v -k "payload"`
Expected: FAIL — no `save_agent_payload` etc.

- [ ] **Step 3: Implement**

Add to `harn/studio.py` (near the other payload functions), importing `roles`/`agentgen`:

```python
def agents_payload(env_dir: Path) -> dict:
    from . import roles as roles_mod
    ags = [{"name": r.name, "command": r.command, "status": r.status,
            "next_status": r.next_status, "trigger": r.trigger, "oracle": r.oracle,
            "isolation": r.isolation, "secrets": r.secrets, "agent": r.agent,
            "model": r.model, "workflow": r.workflow, "body": r.body()}
           for r in roles_mod.discover(env_dir)]
    return {"agents": ags,
            "statuses": [s for s in tasks_mod.lifecycle(env_dir)],
            "skills": [s.name for s in __import__("harn.skills", fromlist=["discover"]).discover(env_dir)],
            "tools": [t.name for t in __import__("harn.tools", fromlist=["discover"]).discover(env_dir)]}


def save_agent_payload(env_dir: Path, body: dict) -> dict:
    from . import roles as roles_mod
    if not (body.get("name") or "").strip():
        return {"ok": False, "error": "name is required"}
    if not (body.get("status") or "").strip():
        return {"ok": False, "error": "status is required"}
    roles_mod.save(env_dir, body)
    return {"ok": True, "name": body["name"]}


def delete_agent_payload(env_dir: Path, body: dict) -> dict:
    from . import roles as roles_mod
    ok = roles_mod.delete(env_dir, (body.get("name") or "").strip())
    return {"ok": ok, "error": None if ok else "no such agent"}


def generate_agent_payload(env_dir: Path, project_root: Path, cfg, body: dict) -> dict:
    from . import agentgen as agentgen_mod
    desc = (body.get("description") or "").strip()
    if not desc:
        return {"ok": False, "error": "description is required"}
    return {"ok": True, "draft": agentgen_mod.generate(env_dir, cfg, desc)}
```

Wire routes in `do_GET` (for `/api/agents`) and `do_POST`:

```python
# do_GET, alongside the other GET routes:
elif route == "/api/agents":
    self._json(agents_payload(env))
# do_POST:
elif route == "/api/agents/save":
    self._json(save_agent_payload(env, body))
elif route == "/api/agents/delete":
    self._json(delete_agent_payload(env, body))
elif route == "/api/agents/generate":
    self._json(generate_agent_payload(env, env.parent, Config.load(env), body))
```

(Check the exact `Config` import name already used in studio.py and match it; `/api/agents/run` already exists — do not touch it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_agent_builder.py -v -k "payload"`
Expected: 2 passed.

- [ ] **Step 5: Full suite**

Run: `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_agent_builder.py
git commit -m "feat(studio): agents list/save/delete/generate payloads + routes"
```

---

### Task 4: Agents tab UI

**Files:**
- Modify: `harn/studio.py` (`_HTML`)
- Test: `tests/test_agent_builder.py` (string-assertion regression, matching the file's existing pattern)

**Interfaces:**
- Consumes: `GET /api/agents`, `POST /api/agents/{save,delete,generate}` (Task 3).
- Produces: an Agents tab (header button `tabAgents` + `showTab('agents')` branch), a list+editor rendered from `/api/agents`, a "Generate agent" box that calls `/api/agents/generate` and fills the editor from the returned draft (without saving), and a Save that calls `/api/agents/save`.

- [ ] **Step 1: Write the failing test**

```python
def test_studio_html_has_agents_tab():
    from harn import studio
    assert "showTab('agents')" in studio._HTML
    assert "/api/agents/generate" in studio._HTML
    assert "/api/agents/save" in studio._HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_builder.py -v -k agents_tab`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add a header tab button next to the existing ones (near `tabSkills`/`tabTools`):

```html
<button id="tabAgents" onclick="showTab('agents')">Agents</button>
```

Add an `agents` branch to `showTab(t)` and a `renderAgents()`/`renderAgentEditor()` pair modeled on the existing `renderSkills()`/`renderSkillEditor()` (they target `#insp` — Agents can reuse `#insp` like Skills/Tools do, since it's a non-Board tab). Provide:
- a list of agents from `GET /api/agents`,
- an editor with controls for every role field (status/next_status dropdowns from the payload's `statuses`, skill/tool multi-selects from the payload's `skills`/`tools`, a persona textarea, a workflow name field + "Open in Flow canvas" button that sets the active workflow and `showTab('flow')`),
- a "Generate agent" textarea + button calling `POST /api/agents/generate`, then filling the editor fields from `r.draft` (role + workflow) WITHOUT saving — showing `r.draft.dropped` as a note if non-empty,
- a Save button → `POST /api/agents/save` (persists role file; if the draft carried a workflow, save it via the existing workflow-save path), and a Delete → `POST /api/agents/delete`.

Follow the exact JS idioms already in the file (`$`, `post_`, `esc`, tab-switch guards). Keep all interpolated values `esc()`-wrapped.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent_builder.py -v -k agents_tab`
Expected: 1 passed.

- [ ] **Step 5: Full suite**

Run: `python3 -m pytest -q`
Expected: green; if a test asserts the exact set of header tabs, update it to include Agents.

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_agent_builder.py
git commit -m "feat(studio): Agents tab — visual role builder + generate-from-prompt"
```

---

### Task 5: Browser verification (Playwright)

**Files:** none (verification only).

- [ ] **Step 1:** `harn setup` a scratch project, seed a skill and a custom tool, `harn ui`.
- [ ] **Step 2:** Open the Agents tab — verify it renders with an empty list + a generate box.
- [ ] **Step 3:** Create an agent manually (name/status/persona), Save, verify `harn_env/agents/<name>.md` exists on disk and re-reads in the list.
- [ ] **Step 4:** Type a description in the generate box, submit — verify the draft fills the editor (role + a workflow referencing the seeded skill/tool) and that NO file was written until Save (check disk before Save).
- [ ] **Step 5:** Confirm any invented skill/tool name shows in the "dropped" note.
- [ ] **Step 6:** Save the generated agent, open its flow in the canvas, verify the steps appear.
- [ ] **Step 7:** Delete the agent, verify it's gone from the list and disk.
- [ ] **Step 8:** Screenshot the Agents tab (with a generated draft) as proof.

No commit (verification only); any bug found is fixed in the relevant task's files with its tests re-run.
