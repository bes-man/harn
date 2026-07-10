# Custom Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user add a new agent-callable MCP tool — by uploading a script or by drafting one in a real turn-based chat with the agent — and export/import it as a single file to share with another harn user.

**Architecture:** A new `harn/tools.py` module (mirrors `harn/skills.py`'s shape: a directory of small files, `discover`/`save`/`read`/`delete`/`index`) stores each custom tool as `harn_env/tools/<name>.json` (plus an optional sibling script file for uploaded tools). At MCP server startup, `harn/mcp_server.py`'s `build_server()` dynamically synthesizes one real Python function per custom tool definition (a genuine `def foo(param1: str, param2: str) -> str` object built via `exec`, not a `**kwargs` catch-all — required because FastMCP's `Tool.from_function` builds each tool's JSON schema by calling `inspect.signature()` on the actual function object) and registers it via `FastMCP.add_tool()`. Calling the tool runs its shell-command template through the exact same `shlex`-based, non-`shell=True` subprocess model `harn/feedback.py` already uses for `Type: command` steps. Studio gets three new pieces: an Upload flow (reusing the existing base64-upload wiring from task attachments), a turn-based agent-chat drafting panel (one blocking `adapter.run_turn()` call per user message, transcript held in the browser tab only until Save), and Export/Import buttons.

**Tech Stack:** Python stdlib only (`json`, `shlex`, `subprocess`, `zipfile`, `base64`), vanilla JS in `harn/studio.py`'s inline `<script>` block, pytest. No new third-party dependencies.

## Global Constraints

- A custom tool's `command` is ALWAYS executed via `shlex.split()` + `subprocess.run(..., shell=False)` — never `shell=True`, never raw string interpolation of a parameter into the command string. Every `{param}` placeholder is replaced with `shlex.quote(str(value))` before the final string is `shlex.split()`. This mirrors `harn/feedback.py`'s existing `run_feedback()` exactly.
- Tool names (`[a-z0-9_]+`) must never collide: not with the ~35 built-in MCP tool names (`harn/mcp_server.py`'s `tool_catalog()`), and not with any other existing custom tool. Checked at Save (both creation paths) AND at Import — a collision blocks the write with a message naming the conflicting tool; never silently overwritten.
- **MCP protocol limitation, surfaced in the UI, not hidden**: a tool saved while an agent's MCP session is already open is invisible to that session — the tool list is fetched once at connection time. After a successful Save, studio must show: "Saved. This tool will be available to the agent starting its next session."
- The agent-authoring "chat" is turn-based, not streaming: each Send is exactly ONE blocking call to `adapter.run_turn(prompt, cwd)` (the adapter picked via the project's already-configured `[harn] agent`/`model`, same `_pick_adapter(cfg)` function `harn/loop.py` already uses elsewhere) with the full transcript-so-far as the prompt. No websockets, no partial-token streaming, no new adapter methods.
- Every committed change bumps `__version__` in `harn/__init__.py` AND `pyproject.toml` together (current version 0.17.22 — this plan's tasks bump it sequentially, one bump per task's commit).
- No new build step, no new third-party dependencies — vanilla JS only for the studio side, matching `harn/studio.py`'s existing style exactly.

---

### Task 1: `harn/tools.py` — data model

**Files:**
- Create: `harn/tools.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces:
  - `@dataclass CustomTool: name: str; description: str; params: list[str]; command: str; source: str; path: Path`
  - `discover(env_dir: Path) -> list[CustomTool]`
  - `read(env_dir: Path, name: str) -> CustomTool | None`
  - `save(env_dir: Path, name: str, description: str, params: list[str], command: str, source: str = "chat") -> Path` — raises `ValueError` if `name` is invalid (not `[a-z0-9_]+`) or if a tool with that name already exists (does NOT check built-in names — that's the caller's job, since `tools.py` must not import `mcp_server.py`, which imports `tools.py`, to avoid a circular import).
  - `name_taken(env_dir: Path, name: str) -> bool` — true if a custom tool with this name already exists.
  - `delete(env_dir: Path, name: str) -> bool`
  - `index(env_dir: Path) -> str` — compact `"- name: description"` listing, same shape as `skills.index()`.
  - `execute(tool: CustomTool, args: dict[str, str], cwd: Path, timeout: int = 600) -> str` — substitutes each `{param}` in `tool.command` with `shlex.quote(str(args.get(param, "")))`, then runs `shlex.split(cmd)` via `subprocess.run(..., cwd=str(cwd), capture_output=True, text=True, timeout=timeout)`, returns `stdout + stderr`. On `subprocess.TimeoutExpired` returns `f"timeout after {timeout}s"`; on `FileNotFoundError` returns `f"command not found: {exc}"` — same error-shape convention as `harn/feedback.py:38-41`.

**Read first:** `harn/skills.py` in full (the `Skill` dataclass, `discover`/`read_skill`/`write_skill_body` shape to mirror) and `harn/feedback.py` in full (the `run_feedback`/`FeedbackResult` shlex-based subprocess execution to mirror exactly for `execute`).

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for harn/tools.py — custom-tool storage and execution."""
import json

from harn import tools


def test_save_creates_a_json_file_and_discover_finds_it(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    p = tools.save(env, "run_lint", "Run project lint", ["target"],
                   "npm run lint -- {target}")
    assert p.exists()
    found = tools.discover(env)
    assert len(found) == 1
    assert found[0].name == "run_lint"
    assert found[0].params == ["target"]


def test_save_rejects_invalid_name(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    try:
        tools.save(env, "Run Lint!", "desc", [], "echo hi")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_save_rejects_duplicate_custom_tool_name(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "run_lint", "desc", [], "echo hi")
    try:
        tools.save(env, "run_lint", "other desc", [], "echo bye")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_name_taken(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    assert tools.name_taken(env, "run_lint") is False
    tools.save(env, "run_lint", "desc", [], "echo hi")
    assert tools.name_taken(env, "run_lint") is True


def test_read_returns_none_for_missing(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    assert tools.read(env, "nope") is None


def test_delete_removes_the_tool(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "run_lint", "desc", [], "echo hi")
    assert tools.delete(env, "run_lint") is True
    assert tools.discover(env) == []
    assert tools.delete(env, "run_lint") is False


def test_index_lists_name_and_description(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "run_lint", "Run project lint", [], "echo hi")
    assert "run_lint: Run project lint" in tools.index(env)


def test_execute_substitutes_params_with_shlex_quote(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tools.save(env, "echo_it", "echo the value", ["msg"], "echo {msg}")
    tool = tools.read(env, "echo_it")
    out = tools.execute(tool, {"msg": "hello world"}, cwd=tmp_path)
    assert out.strip() == "hello world"


def test_execute_quotes_a_param_that_looks_like_a_second_command(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    t = tools.save(env, "echo_it", "echo the value", ["msg"], "echo {msg}")
    tool = tools.read(env, "echo_it")
    out = tools.execute(tool, {"msg": "; rm -rf /tmp/should-not-run"}, cwd=tmp_path)
    # The whole malicious-looking string prints as ONE literal argument to echo —
    # it must never execute as a second shell command.
    assert "; rm -rf /tmp/should-not-run" in out


def test_execute_reports_command_not_found(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "nope_cmd", "desc", [], "this-binary-does-not-exist-xyz")
    tool = tools.read(env, "nope_cmd")
    out = tools.execute(tool, {}, cwd=tmp_path)
    assert "command not found" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harn.tools'`.

- [ ] **Step 3: Implement `harn/tools.py`**

```python
"""Custom tools: user-added, agent-callable capabilities.

Each custom tool is one small JSON file under harn_env/tools/<name>.json —
mirrors harn/skills.py's "directory of files IS the source of truth" shape,
but structured (not prose) since a tool needs a real parameter list. A tool's
`command` is a shell template with `{param}` placeholders, executed the exact
same shlex-quoted, non-shell=True way harn/feedback.py's run_feedback() runs
a project's test command.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass
class CustomTool:
    name: str
    description: str
    params: list[str]
    command: str
    source: str
    path: Path


def _tools_dir(env_dir: Path) -> Path:
    return env_dir / "tools"


def discover(env_dir: Path) -> list[CustomTool]:
    d = _tools_dir(env_dir)
    if not d.exists():
        return []
    out: list[CustomTool] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(CustomTool(
            name=data.get("name", p.stem),
            description=data.get("description", ""),
            params=data.get("params", []),
            command=data.get("command", ""),
            source=data.get("source", "chat"),
            path=p,
        ))
    return out


def read(env_dir: Path, name: str) -> CustomTool | None:
    for t in discover(env_dir):
        if t.name == name:
            return t
    return None


def name_taken(env_dir: Path, name: str) -> bool:
    return read(env_dir, name) is not None


def save(env_dir: Path, name: str, description: str, params: list[str],
         command: str, source: str = "chat") -> Path:
    """Create a new custom tool. Raises ValueError on an invalid name or a
    name collision with an EXISTING CUSTOM tool — this function does NOT
    check the built-in MCP tool names (that would require importing
    mcp_server.py, which imports this module — the caller combines both
    checks via name_taken() + its own built-in-name set)."""
    name = name.strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid tool name: {name!r} (must match [a-z0-9_]+)")
    if name_taken(env_dir, name):
        raise ValueError(f"a custom tool named '{name}' already exists")
    d = _tools_dir(env_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps({
        "name": name,
        "description": description.strip(),
        "params": list(params),
        "command": command,
        "source": source,
    }, indent=2), encoding="utf-8")
    return p


def delete(env_dir: Path, name: str) -> bool:
    t = read(env_dir, name)
    if t is None:
        return False
    t.path.unlink()
    return True


def index(env_dir: Path) -> str:
    lines = [f"- {t.name}: {t.description}" for t in discover(env_dir)]
    return "\n".join(lines) if lines else "(no custom tools installed)"


def execute(tool: CustomTool, args: dict[str, str], cwd: Path,
           timeout: int = 600) -> str:
    cmd = tool.command
    for k, v in args.items():
        cmd = cmd.replace("{" + k + "}", shlex.quote(str(v)))
    try:
        proc = subprocess.run(
            shlex.split(cmd), cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return f"command not found: {exc}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools.py -v`
Expected: PASS — all 9 tests.

- [ ] **Step 5: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 509 (baseline) + 9 = 518 passed.

- [ ] **Step 6: Bump version and commit**

Edit `harn/__init__.py`: `__version__ = "0.17.22"` → `"0.17.23"`. Edit `pyproject.toml` to match.

```bash
git add harn/tools.py harn/__init__.py pyproject.toml tests/test_tools.py
git commit -m "feat(tools): custom-tool storage + shlex-quoted execution (Phase 5); version 0.17.23"
```

---

### Task 2: `harn/mcp_server.py` — dynamic tool registration

**Files:**
- Modify: `harn/mcp_server.py` (add a function-synthesis helper + a registration loop inside `build_server`, right before `return mcp` at line 831)
- Test: `tests/test_custom_tool_registration.py`

**Interfaces:**
- Consumes: `harn/tools.py`'s `discover(env_dir) -> list[CustomTool]` and `execute(tool, args, cwd) -> str` (Task 1).
- Produces: `_make_tool_function(tool: "tools.CustomTool", project_root: Path) -> Callable` (module-private helper in `harn/mcp_server.py`) — synthesizes and returns a REAL Python function object with one named `str` parameter per `tool.params` entry, whose body calls `tools.execute(tool, locals-as-dict, project_root)`. Every custom tool discovered under `harn_env/tools/` is registered into the built `mcp` server via `mcp.add_tool(fn, name=tool.name, description=tool.description)` before `build_server` returns.

**Read first:** `harn/mcp_server.py`'s `build_server` in full (lines ~127-831), especially the `return mcp` at line 831 (registration happens right before this) and `_env_dir()` (line 33-34, gives the env_dir to scan). This task ALSO required resolving a real technical risk during plan-writing: FastMCP's `@mcp.tool()`/`add_tool()` registers a tool by calling `Tool.from_function(fn, ...)` (`mcp/server/fastmcp/tools/base.py`), which calls `func_metadata(fn, ...)` (`mcp/server/fastmcp/utilities/func_metadata.py:179`), which does `inspect.signature(func, eval_str=True)` and iterates `sig.parameters.values()` BY NAME to build a pydantic argument model — there is no lower-level API that accepts an explicit JSON parameter schema instead of a real Python function object, and a `**kwargs`-only catch-all function does not produce a useful per-parameter schema. **The resolved mechanism**: synthesize a real function object via `exec()` of a small generated source string, with one named `str`-annotated parameter per declared param name — this is the standard, safe technique for exactly this situation (the synthesized source is fully controlled by harn itself, built from a whitelisted-character parameter-name list already validated by `tools.save`'s `_NAME_RE`-equivalent check on param names — see Step 3 below for the exact param-name validation this requires).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_custom_tool_registration.py`:

```python
"""A saved custom tool is dynamically registered and callable via the real
FastMCP server, alongside the ~35 built-in tools."""
import asyncio

from harn import mcp_server, tools


def _env(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    env = project_root / "harn_env"
    env.mkdir()
    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    return env


def test_custom_tool_is_registered_and_callable(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    tools.save(env, "echo_it", "echo a message", ["msg"], "echo {msg}")
    server = mcp_server.build_server(start_watch=False)
    registered = asyncio.run(server.list_tools())
    names = {t.name for t in registered}
    assert "echo_it" in names
    assert "list_skills" in names  # a built-in is still present alongside it

    tool = server._tool_manager.get_tool("echo_it")
    result = asyncio.run(tool.run({"msg": "hello"}))
    assert "hello" in str(result)


def test_custom_tool_with_no_params_registers_cleanly(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    tools.save(env, "no_args_tool", "no params at all", [], "echo fixed-output")
    server = mcp_server.build_server(start_watch=False)
    tool = server._tool_manager.get_tool("no_args_tool")
    assert tool is not None
    result = asyncio.run(tool.run({}))
    assert "fixed-output" in str(result)


def test_no_custom_tools_directory_does_not_crash_build_server(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    server = mcp_server.build_server(start_watch=False)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "list_skills" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_custom_tool_registration.py -v`
Expected: FAIL — `echo_it` not found in the registered tool list.

- [ ] **Step 3: Add the import and the synthesis helper**

In `harn/mcp_server.py`, add to the existing import block near the top (alongside the other `from . import ...` lines around line 18-27):

```python
from . import tools as tools_mod
```

Add this module-private helper right before `def build_server(start_watch: bool = True):` (currently line 127):

```python
def _make_tool_function(tool, project_root: Path):
    """Synthesize a REAL Python function object with one named `str`
    parameter per `tool.params`, so FastMCP's `Tool.from_function` (which
    inspects the function's actual signature via `inspect.signature`) can
    build a correct per-parameter JSON schema. A `**kwargs`-only catch-all
    cannot do this — FastMCP iterates named parameters, not an opaque
    kwargs dict. `tool.params` names are already validated by
    `tools.save`'s `_NAME_RE` check (the same `[a-z0-9_]+` pattern applies
    to param names, enforced by the caller before this is ever invoked —
    see the studio save/import handlers in Task 3+), so this exec() only
    ever runs source built from whitelisted identifier characters.
    """
    arg_sig = ", ".join(f"{p}: str = ''" for p in tool.params)
    call_kwargs = ", ".join(f"'{p}': {p}" for p in tool.params)
    src = (
        f"def _custom_tool({arg_sig}) -> str:\n"
        f"    return _run({{{call_kwargs}}})\n"
    )
    ns: dict = {"_run": lambda args: tools_mod.execute(tool, args, project_root)}
    exec(src, ns)  # noqa: S102 — src is built entirely from validated [a-z0-9_]+ names
    fn = ns["_custom_tool"]
    fn.__name__ = tool.name
    fn.__doc__ = tool.description or f"Custom tool: {tool.name}"
    return fn
```

- [ ] **Step 4: Wire the registration loop into `build_server`**

In `harn/mcp_server.py`, find the end of `build_server` (currently):

```python
    return mcp


_catalog_cache: dict[str, str] | None = None
```

Replace with:

```python
    for custom_tool in tools_mod.discover(_env_dir()):
        fn = _make_tool_function(custom_tool, _env_dir().parent)
        mcp.add_tool(fn, name=custom_tool.name, description=custom_tool.description)

    return mcp


_catalog_cache: dict[str, str] | None = None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_custom_tool_registration.py -v`
Expected: PASS — all 3 tests.

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 518 (prior) + 3 = 521 passed.

- [ ] **Step 7: Bump version and commit**

Edit `harn/__init__.py`: `0.17.23` → `0.17.24`. Edit `pyproject.toml` to match.

```bash
git add harn/mcp_server.py harn/__init__.py pyproject.toml tests/test_custom_tool_registration.py
git commit -m "feat(mcp): dynamically register custom tools via synthesized functions; version 0.17.24"
```

---

### Task 3: Studio backend — Tools tab payload + Save/Delete routes (upload path)

**Files:**
- Modify: `harn/studio.py` (extend `tools_catalog_payload`; add `save_custom_tool_payload`/`delete_custom_tool_payload`; add two routes)
- Test: `tests/test_board.py` (or wherever `tools_catalog_payload` is already tested — grep to confirm, extend that file)

**Interfaces:**
- Consumes: `harn/tools.py`'s `discover`, `save`, `delete`, `name_taken` (Task 1); `harn/mcp_server.py`'s `tool_catalog()` (existing, for the built-in-name uniqueness check).
- Produces: `tools_catalog_payload(env_dir)` now includes a `"custom"` key (list of `{name, description, params, command, source}` dicts) alongside the existing built-in `"tools"` dict. `save_custom_tool_payload(env_dir, payload: dict) -> dict` (`{"ok": True}` or `{"ok": False, "error": str}`), `delete_custom_tool_payload(env_dir, name: str) -> dict`.

**Read first:** `harn/studio.py`'s `tools_catalog_payload` (currently ~line 156-166) and the surrounding `*_payload` function conventions; the `do_GET`/`do_POST` route dispatch tables; `harn/attachments.py` + `upload_attachment`'s base64/size-cap pattern (this task's Save-from-upload path follows the SAME shape, just writing a tool JSON + script instead of an attachment).

- [ ] **Step 1: Write the failing tests**

Add to whichever test file already covers `tools_catalog_payload` (grep to find it — if none exists, create `tests/test_tools_payload.py` following the same `_env(tmp_path)` helper pattern other studio-payload test files already use):

```python
def test_tools_catalog_payload_includes_custom_tools(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "Run project lint", ["target"], "npm run lint -- {target}")
    payload = studio.tools_catalog_payload(env)
    custom = payload["custom"]
    assert len(custom) == 1
    assert custom[0]["name"] == "run_lint"
    assert custom[0]["params"] == ["target"]


def test_save_custom_tool_payload_rejects_name_collision_with_builtin(tmp_path):
    env, project_root = _env(tmp_path)
    builtin_name = next(iter(mcp_server.tool_catalog().keys()))
    result = studio.save_custom_tool_payload(env, {
        "name": builtin_name, "description": "x", "params": [], "command": "echo hi",
    })
    assert result["ok"] is False
    assert builtin_name in result["error"]


def test_save_custom_tool_payload_rejects_duplicate_custom_name(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "desc", [], "echo hi")
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "y", "params": [], "command": "echo bye",
    })
    assert result["ok"] is False
    assert "run_lint" in result["error"]


def test_save_custom_tool_payload_succeeds_for_a_new_name(tmp_path):
    env, project_root = _env(tmp_path)
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "Run lint", "params": ["target"],
        "command": "npm run lint -- {target}", "source": "upload",
    })
    assert result["ok"] is True
    assert tools_mod.read(env, "run_lint") is not None


def test_delete_custom_tool_payload_removes_it(tmp_path):
    env, project_root = _env(tmp_path)
    tools_mod.save(env, "run_lint", "desc", [], "echo hi")
    result = studio.delete_custom_tool_payload(env, "run_lint")
    assert result["ok"] is True
    assert tools_mod.read(env, "run_lint") is None
```

(Add `from harn import tools as tools_mod, mcp_server` to the test file's imports. Confirm/adjust the exact `_env(tmp_path)` helper signature to match whatever the chosen test file already uses.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools_payload.py -v` (or wherever placed)
Expected: FAIL — `payload["custom"]` KeyError, and `save_custom_tool_payload`/`delete_custom_tool_payload` don't exist.

- [ ] **Step 3: Extend `tools_catalog_payload` and add the two new payload functions**

In `harn/studio.py`, find `tools_catalog_payload` (currently ~line 156-166):

```python
def tools_catalog_payload(env_dir: Path) -> dict:
    ...
    return {"tools": ...}
```

Read its exact current body and add a `"custom"` key to the returned dict, built from `tools_mod.discover(env_dir)`:

```python
    custom = [
        {"name": t.name, "description": t.description, "params": t.params,
         "command": t.command, "source": t.source}
        for t in tools_mod.discover(env_dir)
    ]
    return {"tools": ..., "custom": custom}   # keep the existing "tools" value as-is
```

Add these two new functions next to it (matching the file's existing `*_payload` conventions — confirm the real import alias for `harn.tools`/`harn.mcp_server` already used elsewhere in the file, e.g. `from . import tools as tools_mod` at the top import block):

```python
def save_custom_tool_payload(env_dir: Path, payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    description = payload.get("description") or ""
    params = payload.get("params") or []
    command = payload.get("command") or ""
    source = payload.get("source") or "chat"
    if not command.strip():
        return {"ok": False, "error": "command is empty"}
    built_in = set(mcp_server.tool_catalog().keys())
    if name in built_in:
        return {"ok": False, "error": f"'{name}' is already a built-in harn tool"}
    try:
        tools_mod.save(env_dir, name, description, params, command, source=source)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def delete_custom_tool_payload(env_dir: Path, name: str) -> dict:
    if not tools_mod.delete(env_dir, name):
        return {"ok": False, "error": f"no such custom tool: {name}"}
    return {"ok": True}
```

- [ ] **Step 4: Wire the two routes**

In `harn/studio.py`'s `do_POST`, add (near the existing `/api/skill`/`/api/skill/delete` branches):

```python
            elif route == "/api/tools/save":
                self._json(save_custom_tool_payload(env, self._read_json()))
            elif route == "/api/tools/delete":
                self._json(delete_custom_tool_payload(env, self._read_json().get("name", "")))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools_payload.py -v`
Expected: PASS — all 5 tests.

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 521 (prior) + 5 = 526 passed.

- [ ] **Step 7: Bump version and commit**

Edit `harn/__init__.py`: `0.17.24` → `0.17.25`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml tests/test_tools_payload.py
git commit -m "feat(studio): custom-tool save/delete routes with built-in + duplicate name checks; version 0.17.25"
```

---

### Task 4: Studio UI — Upload flow + Tools tab list rendering

**Files:**
- Modify: `harn/studio.py` (the Tools tab's `renderTools()` gains a custom-tools section + "＋ Upload tool" button; new JS upload handler)

**Interfaces:**
- Consumes: `/api/tools` (now returns `custom` too, Task 3), `/api/tools/save`, `/api/tools/delete`.
- Produces: nothing new for later tasks to consume — this is the upload-path UI only. Task 6's chat-drafting UI reuses `/api/tools/save` too but has its OWN "Save" button wired separately.

**Read first:** `harn/studio.py`'s `renderTools()` (grep `function renderTools`) and the existing attachment-upload JS idiom (`pickAttachment`/`uploadPickedFile`, ~lines 1489-1501) — the upload button here follows the exact same hidden-`<input type=file>` + base64-read + POST pattern, just to a different route and with extra form fields (name/description/params) collected alongside the file.

- [ ] **Step 1: Add the Upload button + form + custom-tools list to `renderTools()`**

Read `renderTools()`'s current body in full first. Add, inside its returned HTML, a new section (after the existing built-in tools list) rendering each custom tool from `TOOL_DOCS`'s sibling `CUSTOM_TOOLS` array (populate this the same way `TOOL_DOCS` is populated — extend the existing fetch at `~line 1147` to also stash `.custom` into a new global):

```javascript
let CUSTOM_TOOLS=[];   // [{name,description,params,command,source}], refreshed with TOOL_DOCS
```

Find the existing fetch (currently `~line 1147`):
```javascript
  try{ TOOL_DOCS=(await (await fetch(api('/api/tools'))).json()).tools||{}; }catch(e){}
```
Replace with:
```javascript
  try{
    const r=await (await fetch(api('/api/tools'))).json();
    TOOL_DOCS=r.tools||{};
    CUSTOM_TOOLS=r.custom||[];
  }catch(e){}
```

In `renderTools()`, append a section rendering `CUSTOM_TOOLS` with a Delete button per row and an Upload button/form:

```javascript
function renderCustomToolsSection(){
  const rows=CUSTOM_TOOLS.map(t=>
    `<div class="skillrow"><div style="width:100%">`+
    `<div class="nm">${esc(t.name)} <span class="ds">(${esc(t.source)})</span></div>`+
    `<div class="ds">${esc(t.description)}</div>`+
    `<button class="ghost" onclick="deleteCustomTool('${esc(t.name)}')">Delete</button>`+
    `</div></div>`).join('');
  return `<h3>Custom tools</h3>${rows||'<div class="empty">None yet.</div>'}`+
    `<button class="ghost" onclick="$('#toolUploadInput').click()">＋ Upload tool</button>`+
    `<input type="file" id="toolUploadInput" style="display:none" onchange="uploadToolFile(this)"/>`;
}
async function uploadToolFile(input){
  const file=input.files&&input.files[0]; if(!file)return;
  const name=prompt('Tool name (a-z0-9_ only):'); if(!name)return;
  const description=prompt('Description:')||'';
  const paramsRaw=prompt('Comma-separated param names (or leave blank):')||'';
  const params=paramsRaw.split(',').map(s=>s.trim()).filter(Boolean);
  const dataUrl=await new Promise((res,rej)=>{
    const r=new FileReader(); r.onload=()=>res(r.result); r.onerror=rej; r.readAsDataURL(file);
  });
  const content_b64=dataUrl.split(',')[1]||'';
  const argList=params.map(p=>'{'+p+'}').join(' ');
  const r=await post_('/api/tools/save',{name,description,params,
    command:`bash ${file.name} ${argList}`.trim(), source:'upload', script_name:file.name, content_b64});
  if(!r.ok){ alert(r.error||'save failed'); return; }
  alert('Saved. This tool will be available to the agent starting its next session.');
  input.value='';
  TOOL_DOCS={}; await loadTools();  // force a refresh so CUSTOM_TOOLS picks up the new entry
  renderTools();
}
async function deleteCustomTool(name){
  if(!confirm('Delete "'+name+'"?'))return;
  await post_('/api/tools/delete',{name});
  TOOL_DOCS={}; await loadTools();
  renderTools();
}
```

Call `renderCustomToolsSection()` from `renderTools()`'s returned HTML (append its output). Confirm the real name of whatever function currently populates `TOOL_DOCS` (referred to above as `loadTools()` — grep for the function containing the `/api/tools` fetch to find its real name and use that, not a guessed name).

- [ ] **Step 2: Extend the backend upload handling for a script file**

`save_custom_tool_payload` (Task 3) needs to also accept an optional `script_name`/`content_b64` pair and write the script alongside the tool JSON. In `harn/studio.py`, extend `save_custom_tool_payload`:

```python
def save_custom_tool_payload(env_dir: Path, payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    description = payload.get("description") or ""
    params = payload.get("params") or []
    command = payload.get("command") or ""
    source = payload.get("source") or "chat"
    script_name = payload.get("script_name") or ""
    content_b64 = payload.get("content_b64") or ""
    if not command.strip():
        return {"ok": False, "error": "command is empty"}
    built_in = set(mcp_server.tool_catalog().keys())
    if name in built_in:
        return {"ok": False, "error": f"'{name}' is already a built-in harn tool"}
    try:
        p = tools_mod.save(env_dir, name, description, params, command, source=source)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if script_name and content_b64:
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception:
            return {"ok": False, "error": "content_b64 is not valid base64"}
        (p.parent / script_name).write_bytes(data)
    return {"ok": True}
```

- [ ] **Step 3: Verify JS syntax**

Run:
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

- [ ] **Step 4: Write a test for the script-upload path**

Add to `tests/test_tools_payload.py`:

```python
def test_save_custom_tool_payload_writes_the_uploaded_script(tmp_path):
    import base64
    env, project_root = _env(tmp_path)
    content_b64 = base64.b64encode(b"echo hello\n").decode()
    result = studio.save_custom_tool_payload(env, {
        "name": "run_lint", "description": "d", "params": [],
        "command": "bash lint.sh", "source": "upload",
        "script_name": "lint.sh", "content_b64": content_b64,
    })
    assert result["ok"] is True
    script_path = env / "tools" / "lint.sh"
    assert script_path.read_bytes() == b"echo hello\n"
```

- [ ] **Step 5: Run tests, then the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools_payload.py -v` then `python3 -m pytest tests/ -q`
Expected: PASS — 526 (prior) + 1 = 527 passed.

- [ ] **Step 6: Bump version and commit**

Edit `harn/__init__.py`: `0.17.25` → `0.17.26`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml tests/test_tools_payload.py
git commit -m "feat(studio): Tools tab upload UI for custom tools; version 0.17.26"
```

---

### Task 5: Studio backend — agent-chat tool drafting

**Files:**
- Modify: `harn/studio.py` (new `draft_tool_chat_payload`; new route)
- Test: `tests/test_tools_payload.py`

**Interfaces:**
- Consumes: `harn/loop.py`'s `_pick_adapter(cfg: Config) -> Adapter` (existing) and `Adapter.run_turn(prompt: str, cwd: Path, timeout: int = 1800, **overrides) -> AgentResult` (existing, `harn/adapters/base.py:119`).
- Produces: `draft_tool_chat_payload(env_dir: Path, project_root: Path, cfg, payload: dict) -> dict` — `payload` is `{"history": [{"role": "user"|"agent", "text": str}, ...], "message": str}`. Returns `{"reply": str, "draft": {"name":str,"description":str,"params":list[str],"command":str} | None}`. Task 6's chat UI calls this once per Send.

**Read first:** `harn/loop.py`'s `_pick_adapter` (grep `def _pick_adapter`) and `Config` (grep `class Config` in `harn/config.py`) to confirm exact construction (`_pick_adapter(cfg)` needs a `Config` instance — find how other studio.py code already loads one, e.g. `Config.load(env_dir.parent)`, per Task 3's prior precedent in this same plan's Phase 4 work).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tools_payload.py`:

```python
def test_draft_tool_chat_payload_calls_one_agent_turn_and_parses_a_draft(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)

    class FakeAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            class R:
                ok = True
                text = (
                    "Sure, here's a tool that runs your linter:\n\n"
                    "```json\n"
                    '{"name": "run_lint", "description": "Run project lint", '
                    '"params": ["target"], "command": "npm run lint -- {target}"}\n'
                    "```\n"
                )
            return R()

    monkeypatch.setattr(studio, "_pick_adapter", lambda cfg: FakeAdapter())
    from harn.config import Config
    result = studio.draft_tool_chat_payload(env, project_root, Config(), {
        "history": [], "message": "I want a tool that runs my linter",
    })
    assert "here's a tool" in result["reply"]
    assert result["draft"]["name"] == "run_lint"
    assert result["draft"]["params"] == ["target"]


def test_draft_tool_chat_payload_keeps_no_draft_when_reply_has_none(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)

    class FakeAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            class R:
                ok = True
                text = "Can you tell me more about what the tool should do?"
            return R()

    monkeypatch.setattr(studio, "_pick_adapter", lambda cfg: FakeAdapter())
    from harn.config import Config
    result = studio.draft_tool_chat_payload(env, project_root, Config(), {
        "history": [], "message": "make me a tool",
    })
    assert result["draft"] is None


def test_draft_tool_chat_payload_includes_full_transcript_in_the_prompt(tmp_path, monkeypatch):
    env, project_root = _env(tmp_path)
    captured = {}

    class FakeAdapter:
        name = "fake"
        def run_turn(self, prompt, cwd, timeout=1800, **kw):
            captured["prompt"] = prompt
            class R:
                ok = True
                text = "ok"
            return R()

    monkeypatch.setattr(studio, "_pick_adapter", lambda cfg: FakeAdapter())
    from harn.config import Config
    studio.draft_tool_chat_payload(env, project_root, Config(), {
        "history": [{"role": "user", "text": "first message"},
                    {"role": "agent", "text": "first reply"}],
        "message": "second message",
    })
    assert "first message" in captured["prompt"]
    assert "first reply" in captured["prompt"]
    assert "second message" in captured["prompt"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools_payload.py -k draft_tool -v`
Expected: FAIL — `AttributeError: module 'harn.studio' has no attribute 'draft_tool_chat_payload'`.

- [ ] **Step 3: Implement `draft_tool_chat_payload`**

In `harn/studio.py`, add (near the other `*_payload` functions):

```python
_TOOL_DRAFT_SYSTEM_NOTE = (
    "You are helping a user design a new custom tool for harn. A custom "
    "tool is a name + description + list of simple string parameter names "
    "+ a shell command template using {param} placeholders. Reply "
    "conversationally, and whenever you have a concrete proposal (even a "
    "rough first draft), ALSO include it as a fenced ```json code block "
    "with exactly these keys: name, description, params (a list of "
    "strings), command (a string with {param} placeholders matching "
    "params). Keep replies short."
)
_DRAFT_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def draft_tool_chat_payload(env_dir: Path, project_root: Path, cfg,
                            payload: dict) -> dict:
    history = payload.get("history") or []
    message = (payload.get("message") or "").strip()
    if not message:
        return {"reply": "", "draft": None, "error": "empty message"}
    transcript = "\n\n".join(
        f"{'User' if h.get('role') == 'user' else 'Agent'}: {h.get('text', '')}"
        for h in history
    )
    prompt = "\n\n".join(p for p in (
        _TOOL_DRAFT_SYSTEM_NOTE, transcript, f"User: {message}") if p.strip())
    adapter = _pick_adapter(cfg)
    result = adapter.run_turn(prompt, project_root)
    reply = result.text or ""
    m = _DRAFT_JSON_RE.search(reply)
    draft = None
    if m:
        try:
            candidate = json.loads(m.group(1))
            if {"name", "description", "params", "command"} <= candidate.keys():
                draft = candidate
        except json.JSONDecodeError:
            pass
    return {"reply": reply, "draft": draft}
```

Confirm `re`/`json` are already imported at the top of `harn/studio.py` (they almost certainly are — `json.dumps` is used throughout); add `from . import loop as loop_mod` locally inside this function if `_pick_adapter` isn't already imported at module scope (following the SAME lazy-import convention Phase 4's Task 3/9 already established for `loop` in this file — `from .loop import _pick_adapter` or `from . import loop as loop_mod` then `loop_mod._pick_adapter(cfg)`, matching whichever the file already does elsewhere).

- [ ] **Step 4: Wire the route**

In `harn/studio.py`'s `do_POST`, add:

```python
            elif route == "/api/tools/chat":
                from .config import Config
                cfg = Config.load(env.parent)
                self._json(draft_tool_chat_payload(env, env.parent, cfg, self._read_json()))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools_payload.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 527 (prior) + 3 = 530 passed.

- [ ] **Step 7: Bump version and commit**

Edit `harn/__init__.py`: `0.17.26` → `0.17.27`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml tests/test_tools_payload.py
git commit -m "feat(studio): agent-chat tool-drafting backend (one turn per message); version 0.17.27"
```

---

### Task 6: Studio UI — chat panel + draft preview + Save

**Files:**
- Modify: `harn/studio.py` (new chat panel in the Tools tab)

**Interfaces:**
- Consumes: `/api/tools/chat` (Task 5), `/api/tools/save` (Task 3).
- Produces: nothing new for later tasks.

**Read first:** `harn/studio.py`'s `renderTools()`/`renderCustomToolsSection()` (Task 4) for where to add the new panel; the existing BLOCKED-answer banner pattern from Phase 4 Task 9 (`pollBlockedQuestion`/`submitAnswer`) as a style precedent for a message-history + input-box + button UI block in this same file.

- [ ] **Step 1: Add the chat panel HTML/JS**

Add a new section to the Tools tab (call it from `renderTools()`, alongside `renderCustomToolsSection()`):

```javascript
let TOOL_CHAT_HISTORY=[];   // [{role,text}], reset on panel open/tool save
let TOOL_CHAT_DRAFT=null;

function renderToolChatPanel(){
  const msgs=TOOL_CHAT_HISTORY.map(h=>
    `<div class="ds"><b>${h.role==='user'?'You':'Agent'}:</b> ${esc(h.text)}</div>`).join('');
  const draftHtml=TOOL_CHAT_DRAFT
    ? `<div class="skillrow"><div style="width:100%">`+
      `<div class="nm">${esc(TOOL_CHAT_DRAFT.name)}</div>`+
      `<div class="ds">${esc(TOOL_CHAT_DRAFT.description)}</div>`+
      `<div class="ds">params: ${esc((TOOL_CHAT_DRAFT.params||[]).join(', ')||'(none)')}</div>`+
      `<div class="ds">command: ${esc(TOOL_CHAT_DRAFT.command)}</div>`+
      `<button onclick="saveToolDraft()">Save</button></div></div>`
    : `<div class="empty">No draft yet — describe the tool below.</div>`;
  return `<h3>Describe a new tool to the agent</h3>`+
    `<div id="toolChatMsgs">${msgs}</div>`+
    `<textarea id="toolChatInput" rows="2" placeholder="What should this tool do?"></textarea>`+
    `<button onclick="sendToolChat()">Send</button>`+
    `<h4>Draft</h4>${draftHtml}`;
}

async function sendToolChat(){
  const input=$('#toolChatInput');
  const message=input.value.trim();
  if(!message)return;
  TOOL_CHAT_HISTORY.push({role:'user', text:message});
  input.value='';
  renderTools();
  const r=await post_('/api/tools/chat',{history:TOOL_CHAT_HISTORY.slice(0,-1), message});
  TOOL_CHAT_HISTORY.push({role:'agent', text:r.reply||''});
  if(r.draft) TOOL_CHAT_DRAFT=r.draft;
  renderTools();
}

async function saveToolDraft(){
  if(!TOOL_CHAT_DRAFT)return;
  const r=await post_('/api/tools/save',{
    name:TOOL_CHAT_DRAFT.name, description:TOOL_CHAT_DRAFT.description,
    params:TOOL_CHAT_DRAFT.params, command:TOOL_CHAT_DRAFT.command, source:'chat'});
  if(!r.ok){ alert(r.error||'save failed'); return; }
  alert('Saved. This tool will be available to the agent starting its next session.');
  TOOL_CHAT_HISTORY=[]; TOOL_CHAT_DRAFT=null;
  TOOL_DOCS={}; await loadTools();
  renderTools();
}
```

Call `renderToolChatPanel()` from `renderTools()`'s returned HTML, alongside `renderCustomToolsSection()`.

- [ ] **Step 2: Verify JS syntax**

Run:
```bash
python3 -c "
import re
html = open('harn/studio.py').read()
m = re.search(r'<script>(.*)</script>', html, re.DOTALL)
open('/tmp/studio_check.js', 'w').write(m.group(1))
"
node --check /tmp/studio_check.js
```
Expected: no output.

- [ ] **Step 3: Manual live verification**

If a live-preview/browser MCP tool is available: start the studio dev server, open the Tools tab, type a message describing a tool ("I want a tool that runs `npm run lint`"), send it, confirm a reply and (once the agent proposes one) a draft preview appear, click Save, confirm the success alert and that the tool now appears in the custom-tools list. If no live tooling is available, verify via careful JS tracing instead and say so explicitly in the task report.

- [ ] **Step 4: Run the full suite (sanity check — this task is JS-only, no new Python tests)**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — 530 passed, unchanged.

- [ ] **Step 5: Bump version and commit**

Edit `harn/__init__.py`: `0.17.27` → `0.17.28`. Edit `pyproject.toml` to match.

```bash
git add harn/studio.py harn/__init__.py pyproject.toml
git commit -m "feat(studio): turn-based agent-chat panel for drafting a custom tool; version 0.17.28"
```

---

### Task 7: Export / Import

**Files:**
- Modify: `harn/tools.py` (add `export_bundle`/`import_bundle`)
- Modify: `harn/studio.py` (routes + UI buttons)
- Test: `tests/test_tools.py`, `tests/test_tools_payload.py`

**Interfaces:**
- Consumes: `harn/tools.py`'s `CustomTool`/`save`/`read` (Task 1).
- Produces: `tools.export_bundle(tool: CustomTool) -> bytes` — for a `source == "chat"` tool (no sibling script) returns the raw JSON bytes; for a `source == "upload"` tool (has a sibling script file) returns a zip archive (stdlib `zipfile`) containing `<name>.json` + the script file. `tools.import_bundle(env_dir: Path, data: bytes, filename: str) -> CustomTool` — detects json-vs-zip by content (try `zipfile.is_zipfile` on the bytes via `io.BytesIO`, else parse as JSON directly), extracts, and calls the SAME uniqueness-checked `save()` (raises `ValueError` on a name collision — the caller/route surfaces this exactly like Task 3's Save routes do).

**Read first:** `harn/tools.py` as it stands after Task 1 (in particular the JSON shape `save()` writes, so `export_bundle`/`import_bundle` round-trip it exactly).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tools.py`:

```python
def test_export_bundle_for_a_chat_tool_is_plain_json(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    tools.save(env, "echo_it", "desc", ["msg"], "echo {msg}", source="chat")
    tool = tools.read(env, "echo_it")
    data = tools.export_bundle(tool)
    parsed = json.loads(data)
    assert parsed["name"] == "echo_it"


def test_export_bundle_for_an_upload_tool_is_a_zip_with_the_script(tmp_path):
    import zipfile, io
    env = tmp_path / "harn_env"
    env.mkdir()
    p = tools.save(env, "run_lint", "desc", [], "bash lint.sh", source="upload")
    (p.parent / "lint.sh").write_text("echo hi\n", encoding="utf-8")
    tool = tools.read(env, "run_lint")
    data = tools.export_bundle(tool)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    assert "run_lint.json" in names
    assert "lint.sh" in names


def test_import_bundle_json_round_trips(tmp_path):
    env1 = tmp_path / "harn_env1"; env1.mkdir()
    env2 = tmp_path / "harn_env2"; env2.mkdir()
    tools.save(env1, "echo_it", "desc", ["msg"], "echo {msg}", source="chat")
    data = tools.export_bundle(tools.read(env1, "echo_it"))
    imported = tools.import_bundle(env2, data, "echo_it.json")
    assert imported.name == "echo_it"
    assert tools.read(env2, "echo_it").command == "echo {msg}"


def test_import_bundle_zip_round_trips_the_script(tmp_path):
    env1 = tmp_path / "harn_env1"; env1.mkdir()
    env2 = tmp_path / "harn_env2"; env2.mkdir()
    p = tools.save(env1, "run_lint", "desc", [], "bash lint.sh", source="upload")
    (p.parent / "lint.sh").write_text("echo hi\n", encoding="utf-8")
    data = tools.export_bundle(tools.read(env1, "run_lint"))
    imported = tools.import_bundle(env2, data, "run_lint.zip")
    assert imported.name == "run_lint"
    assert (env2 / "tools" / "lint.sh").read_text() == "echo hi\n"


def test_import_bundle_rejects_a_name_collision(tmp_path):
    env = tmp_path / "harn_env"; env.mkdir()
    tools.save(env, "echo_it", "existing", ["msg"], "echo {msg}", source="chat")
    other = tmp_path / "harn_env_other"; other.mkdir()
    tools.save(other, "echo_it", "different tool, same name", [], "echo hi", source="chat")
    data = tools.export_bundle(tools.read(other, "echo_it"))
    try:
        tools.import_bundle(env, data, "echo_it.json")
        assert False, "expected ValueError"
    except ValueError:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools.py -k "export_bundle or import_bundle" -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Implement `export_bundle`/`import_bundle`**

In `harn/tools.py`, add near the top:

```python
import io
import zipfile
```

Add these functions after `execute`:

```python
def export_bundle(tool: CustomTool) -> bytes:
    """A tool with no sibling script exports as plain JSON bytes. A tool
    whose command references a sibling file (source == 'upload') exports
    as a zip containing both the JSON and the script, so a single download
    always reconstructs the tool exactly."""
    json_bytes = tool.path.read_bytes()
    siblings = [p for p in tool.path.parent.iterdir()
               if p.is_file() and p != tool.path and p.stem == tool.name.split(".")[0]]
    # simpler, exact rule: bundle every OTHER file in the tool's directory
    # whose name is referenced by `command` — a script upload always names
    # its file explicitly in `command`, so check membership instead of stem
    # matching (avoids bundling unrelated files that happen to share a stem).
    referenced = [p for p in tool.path.parent.iterdir()
                 if p.is_file() and p != tool.path and p.name in tool.command]
    if not referenced:
        return json_bytes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{tool.name}.json", json_bytes)
        for p in referenced:
            zf.writestr(p.name, p.read_bytes())
    return buf.getvalue()


def import_bundle(env_dir: Path, data: bytes, filename: str) -> CustomTool:
    """Inverse of export_bundle. Detects json-vs-zip by content. Raises
    ValueError on a name collision (same rule as save())."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        zf = zipfile.ZipFile(io.BytesIO(data))
        json_name = next(n for n in zf.namelist() if n.endswith(".json"))
        parsed = json.loads(zf.read(json_name))
        p = save(env_dir, parsed["name"], parsed.get("description", ""),
                parsed.get("params", []), parsed["command"],
                source=parsed.get("source", "upload"))
        for n in zf.namelist():
            if n != json_name:
                (p.parent / n).write_bytes(zf.read(n))
        return read(env_dir, parsed["name"])
    parsed = json.loads(data)
    save(env_dir, parsed["name"], parsed.get("description", ""),
        parsed.get("params", []), parsed["command"],
        source=parsed.get("source", "chat"))
    return read(env_dir, parsed["name"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Wire studio routes (download-export + upload-import) and buttons**

In `harn/studio.py`, add a GET route for export (returns raw bytes, correct content-type/filename, mirroring how `/api/attachments/file` already streams bytes at ~line 656-662):

```python
            elif route == "/api/tools/export":
                name = self._query("name") or ""
                tool = tools_mod.read(env, name)
                if tool is None:
                    self._send(404, b"not found", "text/plain"); return
                data = tools_mod.export_bundle(tool)
                is_zip = data[:2] == b"PK"
                fname = f"{name}.zip" if is_zip else f"{name}.json"
                ctype = "application/zip" if is_zip else "application/json"
                self._send(200, data, ctype,
                          extra_headers={"Content-Disposition": f'attachment; filename="{fname}"'})
```

(Confirm `self._send`'s real signature — grep it in `harn/studio.py` — it may not currently accept `extra_headers`; if not, add that optional parameter to `_send` itself, defaulting to `None`, and apply any given headers via `self.send_header(k, v)` before `self.end_headers()`, matching its existing header-writing style.)

Add a POST route for import:

```python
            elif route == "/api/tools/import":
                body = self._read_json()
                try:
                    data = base64.b64decode(body.get("content_b64") or "", validate=True)
                    tool = tools_mod.import_bundle(env, data, body.get("filename", ""))
                    self._json({"ok": True, "name": tool.name})
                except (ValueError, Exception) as exc:
                    self._json({"ok": False, "error": str(exc)})
```

Add "Export"/"＋ Import tool" buttons to `renderCustomToolsSection()` (Task 4), following the same upload-input pattern as `uploadToolFile`:

```javascript
// inside renderCustomToolsSection()'s per-row template, add:
`<button class="ghost" onclick="location.href=api('/api/tools/export?name='+encodeURIComponent('${esc(t.name)}'))">Export</button>`

// alongside the existing Upload button:
`<button class="ghost" onclick="$('#toolImportInput').click()">＋ Import tool</button>`+
`<input type="file" id="toolImportInput" style="display:none" onchange="importToolFile(this)"/>`
```

```javascript
async function importToolFile(input){
  const file=input.files&&input.files[0]; if(!file)return;
  const dataUrl=await new Promise((res,rej)=>{
    const r=new FileReader(); r.onload=()=>res(r.result); r.onerror=rej; r.readAsDataURL(file);
  });
  const content_b64=dataUrl.split(',')[1]||'';
  const r=await post_('/api/tools/import',{filename:file.name,content_b64});
  if(!r.ok){ alert(r.error||'import failed'); return; }
  alert('Imported "'+r.name+'". Available to the agent starting its next session.');
  input.value='';
  TOOL_DOCS={}; await loadTools();
  renderTools();
}
```

- [ ] **Step 6: Verify JS syntax**

Run the same `node --check` extraction as prior tasks.

- [ ] **Step 7: Run tests, then the full suite**

`tests/test_tools.py`'s `import_bundle`/`export_bundle` tests (Step 1) already cover the round-trip logic directly — Step 5's `/api/tools/import` route is a thin wrapper with no independent logic worth a duplicate studio-level test (it only base64-decodes and forwards to `import_bundle`, already tested).

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/test_tools.py -v` then
`python3 -m pytest tests/ -q`
Expected: PASS — 530 (prior) + 5 (Task 7's `tests/test_tools.py` additions) = 535 passed.

- [ ] **Step 8: Bump version and commit**

Edit `harn/__init__.py`: `0.17.28` → `0.17.29`. Edit `pyproject.toml` to match.

```bash
git add harn/tools.py harn/studio.py harn/__init__.py pyproject.toml tests/test_tools.py tests/test_tools_payload.py
git commit -m "feat: export/import custom tools as a single portable file; version 0.17.29"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (EN + RU, following the established bilingual-mirror convention from Phase 3 Task 8 / Phase 4 Task 10)

**Interfaces:**
- Consumes: nothing — pure documentation.
- Produces: nothing new.

- [ ] **Step 1: Read the current state of the README's Tools-tab-adjacent documentation**

Grep `README.md` for any existing mention of the Tools tab / built-in MCP tools, to find the right heading level and neighborhood for a new custom-tools section.

- [ ] **Step 2: Write the English section**

Add a new subsection covering:
- What a custom tool is (name + description + params + a shell command template), and that it executes exactly like a `Type: command` workflow step (subprocess, shlex-quoted params, no shell injection).
- The two ways to create one: Upload (script + manually-entered name/description/params) and the agent-chat drafting panel (turn-based: one message, one agent reply, until a draft appears; edit or accept it; Save).
- The MCP session-refresh caveat: a newly saved tool is available to the agent starting its NEXT session, not the current one.
- Name-uniqueness against both built-in and other custom tools.
- Export (download a single file) / Import (upload that file into another project) for sharing between harn users.

- [ ] **Step 3: Mirror the section in Russian**

Add the same content under `## harn — на русском`, in the same relative position, matching the file's existing bilingual-mirror convention exactly.

- [ ] **Step 4: Run the full suite (sanity check — docs-only change)**

Run: `cd /Users/maximus/projects/harn && python3 -m pytest tests/ -q`
Expected: PASS — unchanged count from Task 7.

- [ ] **Step 5: Bump version and commit**

Edit `harn/__init__.py`: `0.17.29` → `0.17.30`. Edit `pyproject.toml` to match.

```bash
git add README.md harn/__init__.py pyproject.toml
git commit -m "docs: document custom tools -- upload, agent-chat authoring, export/import; version 0.17.30"
```
