"""harn studio — a local visual editor for the workflow + skills (no deps).

`harn ui` serves this on http://127.0.0.1:9999. It renders WORKFLOW.md as an
n8n-style flow of step nodes; clicking a node lets you edit its body, toggle the
skills required at that step, and edit those skills' content — all saved back to
`harn_env/` files. Pure stdlib (`http.server` + a single self-contained HTML page
with vanilla JS), in keeping with harn's lean footprint.

The HTTP layer is thin; the real work is three pure functions —
`state_payload`, `apply_workflow`, `apply_skill` — so they're unit-testable
without binding a socket.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import attachments as attachments_mod
from . import events as events_mod
from . import runner as runner_mod
from . import skills as skills_mod
from . import state as state_mod
from . import tasks as tasks_mod
from . import tools as tools_mod
from . import workflow as workflow_mod
from . import workflows as workflows_mod
from .config import Config
from .loop import _pick_adapter


# --------------------------------------------------------------------------- #
# Pure data layer (no HTTP) — easy to unit-test
# --------------------------------------------------------------------------- #
_LAYOUT_FILE = "studio_layout.json"


def _layout_path(env_dir: Path) -> Path:
    return env_dir / "state" / _LAYOUT_FILE


def layout_payload(env_dir: Path) -> dict:
    """Node positions for the canvas, keyed by node title → {x, y}. UI-only
    metadata kept OUT of WORKFLOW.md (the agent never needs coordinates), in
    harn_env/state/studio_layout.json. Missing/corrupt → empty (auto-layout)."""
    p = _layout_path(env_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def apply_layout(env_dir: Path, payload: dict) -> dict:
    """Persist node positions from a drag/drop. Best-effort; ignores bad input."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "expected an object"}
    clean: dict = {}
    for title, pos in payload.items():
        if isinstance(pos, dict) and "x" in pos and "y" in pos:
            try:
                clean[str(title)] = {"x": round(float(pos["x"])),
                                     "y": round(float(pos["y"]))}
            except (TypeError, ValueError):
                continue
    p = _layout_path(env_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return {"ok": True}


def state_payload(env_dir: Path) -> dict:
    """Everything the editor needs: the parsed workflow nodes (of the ACTIVE
    workflow, mirrored in WORKFLOW.md) + every skill (name, description, body) +
    saved canvas positions + the workflow switcher's presets and active name."""
    parsed = workflow_mod.parse(env_dir)
    sk = []
    for s in skills_mod.discover(env_dir):
        sk.append({"name": s.name, "description": s.description, "body": s.body()})
    return {"workflow": parsed, "skills": sk, "layout": layout_payload(env_dir),
            "workflows": workflows_mod.list_workflows(env_dir),
            "active": workflows_mod.active_name(env_dir)}


def apply_workflow(env_dir: Path, payload: dict) -> dict:
    """Persist edited workflow nodes into the ACTIVE workflow preset (and mirror
    it into WORKFLOW.md so every agent reads it). Echoes back the saved nodes
    (ids now stamped) so the client can sync without a full page reload."""
    name = workflows_mod.save_active(env_dir, {
        "preamble": payload.get("preamble", ""),
        "nodes": payload.get("nodes", []),
    })
    saved = workflows_mod.load(env_dir, name) or {}
    return {"ok": True, "active": name, "workflow": saved}


def list_workflows_payload(env_dir: Path) -> dict:
    return {"workflows": workflows_mod.list_workflows(env_dir),
            "active": workflows_mod.active_name(env_dir)}


def activate_workflow(env_dir: Path, payload: dict) -> dict:
    """Switch the active workflow → re-render WORKFLOW.md → reload returns its
    nodes onto the canvas."""
    name = workflows_mod.activate(env_dir, (payload.get("name") or "").strip())
    return {"ok": True, "active": name}


def create_workflow(env_dir: Path, payload: dict) -> dict:
    """New named preset, seeded from the current default's steps."""
    try:
        m = workflows_mod.create(
            env_dir,
            name=(payload.get("name") or "").strip(),
            title=(payload.get("title") or "").strip(),
            description=(payload.get("description") or "").strip(),
            version=str(payload.get("version") or "1").strip(),
            copy_from=(payload.get("copy_from") or "").strip() or None,
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    # Activate it immediately so the canvas opens on the new preset.
    workflows_mod.activate(env_dir, m["name"])
    return {"ok": True, "workflow": m, "active": m["name"]}


def save_workflow_meta(env_dir: Path, payload: dict) -> dict:
    """Rename / re-describe / re-version a preset (title, description, version)."""
    name = (payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing workflow name"}
    m = workflows_mod.save_meta(
        env_dir, name,
        title=payload.get("title"),
        description=payload.get("description"),
        version=payload.get("version"),
        new_name=payload.get("new_name"),
    )
    return {"ok": True, "workflow": m, "active": workflows_mod.active_name(env_dir)}


def delete_workflow(env_dir: Path, payload: dict) -> dict:
    """Delete a named preset (the default can't be deleted)."""
    name = (payload.get("name") or "").strip()
    if not workflows_mod.delete(env_dir, name):
        return {"ok": False, "error": "cannot delete (default or unknown)"}
    return {"ok": True, "active": workflows_mod.active_name(env_dir)}


def tools_catalog_payload(env_dir: Path) -> dict:
    """Tool descriptions for the Tools tab: each tool's real docstring (the
    same text an agent sees via `tools/list` — never drifts out of sync with
    what a tool actually does) plus, where one exists, a longer human-facing
    note (what/why/when) from tool_notes.py. The note is UI-only — it never
    touches the agent-facing docstring, so the agent's fixed context budget
    is unaffected by how much explanation a human browsing the UI needs."""
    from . import mcp_server, tool_notes
    catalog = mcp_server.tool_catalog()
    custom = [
        {"name": t.name, "description": t.description, "params": t.params,
         "command": t.command, "source": t.source}
        for t in tools_mod.discover(env_dir)
    ]
    return {"tools": {name: tool_notes.merged(name, doc)
                      for name, doc in catalog.items()},
            "custom": custom}


def save_custom_tool_payload(env_dir: Path, payload: dict) -> dict:
    """Persist a new custom tool. Rejects a name collision with a BUILT-IN
    MCP tool name here (mcp_server.tool_catalog() is the source of truth for
    the ~35 built-ins); a collision with an existing CUSTOM tool, or an
    invalid name/param, is independently rejected by tools_mod.save()
    itself, which raises ValueError — both checks gate every Save.

    When the studio's Upload flow supplies an uploaded script
    (script_name + content_b64, base64-encoded file bytes), the script is
    written alongside the tool's JSON definition so the tool's `command`
    (e.g. "bash lint.sh") can find it at the agent's next MCP session —
    custom tools are only re-registered when a new session starts, never
    picked up by one already running."""
    from . import mcp_server
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
        script_name = Path(script_name).name  # strip any path components (traversal guard)
        if not script_name or script_name in (".", ".."):
            return {"ok": False, "error": "invalid script_name"}
        # A tool's script must NEVER be a .json — discover() treats every
        # harn_env/tools/*.json as a live tool definition, so writing a .json
        # sibling here would plant a second tool with an arbitrary command,
        # bypassing tools.save()'s name/param validation, the collision check,
        # and the built-in-name gate. The ONLY .json in tools/ is the one
        # tools.save() wrote above. Mirrors import_bundle()'s second-.json
        # defense on the import path.
        if script_name.lower().endswith(".json"):
            return {"ok": False,
                    "error": "script_name must not be a .json file"}
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception:
            return {"ok": False, "error": "content_b64 is not valid base64"}
        (p.parent / script_name).write_bytes(data)
    return {"ok": True}


def import_custom_tool_bundle_payload(env_dir: Path, payload: dict) -> dict:
    """Import a custom tool exported by another harn user (Phase 5, Task 7).
    `content_b64` is the raw bytes of the exported file (plain-JSON export
    or a zip bundle), base64-encoded by the browser's FileReader before the
    POST -- exactly like the existing upload-script flow in
    save_custom_tool_payload().

    SECURITY: this is attacker-controlled input from another user's
    machine. The built-in-name collision check below mirrors
    save_custom_tool_payload()'s own check (mcp_server.tool_catalog() is
    only importable here, not from tools.py, which mcp_server.py itself
    imports) so an import can't shadow a built-in tool any more than a
    fresh Save can. The actual persistence -- and the injection-shaped
    name/param rejection -- happens inside tools_mod.import_bundle(), which
    routes through the SAME tools_mod.save() gate as every other tool
    creation path; this function does not write the tool's JSON or any
    sibling script itself."""
    from . import mcp_server
    try:
        data = base64.b64decode(payload.get("content_b64") or "", validate=True)
    except Exception:
        return {"ok": False, "error": "content_b64 is not valid base64"}
    if len(data) > _MAX_ATTACHMENT_BYTES:
        mb = _MAX_ATTACHMENT_BYTES // (1024 * 1024)
        return {"ok": False, "error": f"bundle too large (max {mb}MB)"}
    filename = payload.get("filename") or ""
    try:
        parsed_name = _peek_bundle_name(data)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    built_in = set(mcp_server.tool_catalog().keys())
    if parsed_name in built_in:
        return {"ok": False, "error": f"'{parsed_name}' is already a built-in harn tool"}
    try:
        tool = tools_mod.import_bundle(env_dir, data, filename)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": tool.name}


def _peek_bundle_name(data: bytes) -> str:
    """Read just the `name` field out of an export bundle (json or zip)
    without persisting anything -- used only for the built-in-name
    pre-check above. Any parse failure here is surfaced as a ValueError
    with a friendly message; the actual import (and its authoritative
    validation) still happens via tools_mod.import_bundle()."""
    import io
    import zipfile
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            zf = zipfile.ZipFile(io.BytesIO(data))
            try:
                json_name = next(n for n in zf.namelist() if n.endswith(".json"))
            except StopIteration:
                raise ValueError("zip bundle has no .json tool definition")
            parsed = json.loads(zf.read(json_name))
        else:
            parsed = json.loads(data)
        return parsed.get("name", "")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"could not parse tool bundle: {exc}")


def delete_custom_tool_payload(env_dir: Path, name: str) -> dict:
    """Remove a custom tool. Returns ok:False (not a raised error) when the
    name doesn't exist, matching delete_skill's forgiving style elsewhere in
    this file."""
    if not tools_mod.delete(env_dir, name):
        return {"ok": False, "error": f"no such custom tool: {name}"}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Agent-chat tool drafting (Phase 5, Task 5) — one blocking agent turn per
# Send, never streamed. Each call sends the FULL transcript so far (every
# prior user/agent message) so the agent keeps the whole conversation in
# view, exactly like a fresh non-interactive `run_turn` call would need.
# --------------------------------------------------------------------------- #
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
_DRAFT_REQUIRED_KEYS = {"name", "description", "params", "command"}


def draft_tool_chat_payload(env_dir: Path, project_root: Path, cfg: Config,
                             payload: dict) -> dict:
    """One turn of the Studio's tool-drafting chat: send the full transcript
    so far (every prior message, oldest first) plus the new message as a
    single blocking `adapter.run_turn` call, then look for a fenced
    ```json draft in the reply. No streaming, no multi-turn loop here — the
    UI calls this once per Send and re-renders with the result.

    A missing or malformed draft never raises; `draft` is simply None so the
    chat can keep going until the agent produces something parseable."""
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
    agent_name = (payload.get("agent") or "").strip()
    model_name = (payload.get("model") or "").strip()
    if agent_name:
        from .adapters import get_adapter, _REGISTRY
        adapter = get_adapter(agent_name) if agent_name in _REGISTRY else _pick_adapter(cfg)
    else:
        adapter = _pick_adapter(cfg)
    result = adapter.run_turn(prompt, project_root, model=model_name or None)
    reply = result.text or ""
    m = _DRAFT_JSON_RE.search(reply)
    draft = None
    if m:
        try:
            candidate = json.loads(m.group(1))
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict) and _DRAFT_REQUIRED_KEYS <= candidate.keys():
            draft = candidate
    return {"reply": reply, "draft": draft}


def apply_skill(env_dir: Path, payload: dict) -> dict:
    """Persist one edited (or new) skill body."""
    name = (payload.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing skill name"}
    skills_mod.write_skill_body(env_dir, name, payload.get("body", ""),
                                payload.get("description"))
    return {"ok": True}


def delete_skill(env_dir: Path, payload: dict) -> dict:
    """Remove a skill (its SKILL.md directory). The workflow may still reference
    the name harmlessly; the editor also strips it from steps on the next save."""
    import shutil
    name = (payload.get("name") or "").strip().lower().replace(" ", "-")
    if not name:
        return {"ok": False, "error": "missing skill name"}
    d = env_dir / "skills" / name
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Config toggles (semble / socraticode) — read + targeted harn.toml write
# --------------------------------------------------------------------------- #
def config_payload(env_dir: Path) -> dict:
    """The toggles the header shows + the project path."""
    cfg = Config.load(env_dir)
    return {
        "env": str(env_dir),
        "project": cfg.project,
        "toggles": {
            "semble": cfg.code_search_semble,
            "socraticcode": cfg.code_search_socraticcode,
        },
    }


_BOOL = {"semble", "socraticcode"}


def set_config_flag(env_dir: Path, payload: dict) -> dict:
    """Flip a `[code_search]` flag in harn.toml in place (stdlib can't WRITE toml,
    so we do a targeted regex edit — same approach harn uses elsewhere)."""
    key = (payload.get("key") or "").strip()
    if key not in _BOOL:
        return {"ok": False, "error": f"unknown toggle {key!r}"}
    val = bool(payload.get("value"))
    toml = env_dir / "harn.toml"
    text = toml.read_text(encoding="utf-8") if toml.exists() else ""
    line = f"{key} = {'true' if val else 'false'}"
    if re.search(rf"(?m)^\s*{key}\s*=.*$", text):
        text = re.sub(rf"(?m)^\s*{key}\s*=.*$", line, text)
    elif re.search(r"(?m)^\[code_search\]\s*$", text):
        text = re.sub(r"(?m)^\[code_search\]\s*$", f"[code_search]\n{line}", text)
    else:
        text = (text.rstrip() + "\n\n[code_search]\n" + line + "\n") if text else \
            f"[code_search]\n{line}\n"
    toml.parent.mkdir(parents=True, exist_ok=True)
    toml.write_text(text, encoding="utf-8")
    return {"ok": True, "key": key, "value": val}


# --------------------------------------------------------------------------- #
# Per-stage model/effort/temperature overrides (harn.toml's [models.<stage>])
# --------------------------------------------------------------------------- #
def models_payload(env_dir: Path) -> dict:
    """Everything the UI needs to configure harn run's agents/models:

    - `default_agent`/`default_model`: the `[harn]` defaults every step uses
      unless overridden on the step itself — the answer to "when I use
      Cursor, harn run uses Cursor".
    - `agents`: EVERY known agent (not just the chain) with its curated known
      models/efforts/temperatures and whether its CLI is installed here
      (`available`), so both the Settings agent picker and the per-step model
      dropdown can show real, agent-specific options.

    Per-step overrides now live directly on each workflow node's
    `agent`/`model`/`effort`/`temperature` fields (see harn/workflow.py),
    saved via `/api/workflow` or `/api/task_plan` — there is no more
    stage-keyed `[models.<stage>]` config in harn.toml.
    """
    from .adapters import get_adapter, _REGISTRY
    cfg = Config.load(env_dir)
    agents = {}
    for name in _REGISTRY:
        a = get_adapter(name)
        agents[name] = {"model": bool(a.MODEL_FLAG), "effort": bool(a.EFFORT_FLAG),
                        "temperature": bool(a.TEMPERATURE_FLAG),
                        "available": a.available(),
                        "models": list(a.MODELS), "efforts": list(a.EFFORTS),
                        "temperatures": list(a.TEMPERATURES)}
    return {"agent_chain": cfg.agent_chain,
            "default_agent": (cfg.agent_chain[0] if cfg.agent_chain else cfg.agent),
            "default_model": cfg.model,
            "agents": agents}


def _toml_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def save_defaults(env_dir: Path, payload: dict) -> dict:
    """Write the harn run defaults — `[harn] agent` (which CLI drives every
    stage) and `[harn] model` (default model) — via a targeted in-place edit of
    the `[harn]` table (stdlib can't write TOML). This is the setting that makes
    `harn run` use Cursor/Codex/Claude/… as you choose."""
    from .adapters import _REGISTRY
    agent = str(payload.get("agent") or "").strip()
    model = str(payload.get("model") or "").strip()
    if agent and agent not in _REGISTRY:
        return {"ok": False, "error": f"unknown agent {agent!r}"}
    toml_path = env_dir / "harn.toml"
    text = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""

    def _set(text: str, key: str, value: str) -> str:
        line = f'{key} = "{_toml_escape(value)}"'
        if re.search(rf"(?m)^\s*{key}\s*=.*$", text):
            return re.sub(rf"(?m)^\s*{key}\s*=.*$", line, text, count=1)
        if re.search(r"(?m)^\[harn\]\s*$", text):
            return re.sub(r"(?m)^\[harn\]\s*$", f"[harn]\n{line}", text, count=1)
        return (f"[harn]\n{line}\n" + ("\n" + text if text else ""))

    if agent:
        text = _set(text, "agent", agent)
    text = _set(text, "model", model)   # allow clearing to ""
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(text, encoding="utf-8")
    return {"ok": True, "agent": agent, "model": model}


# --------------------------------------------------------------------------- #
# Live progress + per-stage stats (from events.jsonl) for the flow animation
# --------------------------------------------------------------------------- #
# Map a workflow node (by keywords in its title) to the pipeline stage that
# emits events. Nodes that match no stage stay neutral (grey) during a run.
_STAGE_KEYWORDS = [
    ("plan",      ("pre-task", "plan", "clarif")),
    ("ui_verify", ("ui verify", "ui-verify", "browser")),
    ("verify",    ("verify",)),
    ("execute",   ("implement", "execute", "build", "code")),
    ("test",      ("test",)),
    ("oracle",    ("oracle",)),
    ("reconcile", ("reconcile",)),
]


def _node_stage(title: str) -> str | None:
    t = title.lower()
    for stage, kws in _STAGE_KEYWORDS:
        if any(k in t for k in kws):
            return stage
    return None


def progress_payload(env_dir: Path) -> dict:
    """Per-node status + per-stage stats for the LATEST run, from events.jsonl.

    status per stage: 'active' (running), 'done' (finished, run still going),
    'complete' (run ended ok), else 'pending'. Stats: time/tokens/cost per stage
    + run totals — the raw material for the on-canvas stats block and $-estimate.
    """
    evs = events_mod.read(env_dir)
    if not evs:
        return {"run": None, "stages": {}, "totals": {}, "active": None, "ended": False}
    last_run = None
    for e in evs:
        if e.get("event") == "run_start":
            last_run = e.get("run_id")
    run = [e for e in evs if e.get("run_id") == last_run] if last_run else evs
    stages: dict[str, dict] = {}
    active = None
    ended = False
    end_phase = None
    for e in run:
        ev, st = e.get("event"), e.get("stage")
        if ev == "stage_start" and st:
            stages.setdefault(st, {})["status"] = "active"
            active = st
        elif ev == "stage_end" and st:
            s = stages.setdefault(st, {})
            s["status"] = "done"
            s["dur_ms"] = (s.get("dur_ms") or 0) + (e.get("dur_ms") or 0)
            s["tok_in"] = (s.get("tok_in") or 0) + (e.get("tok_in") or 0)
            s["tok_out"] = (s.get("tok_out") or 0) + (e.get("tok_out") or 0)
            if e.get("cost_usd") is not None:
                s["cost_usd"] = round((s.get("cost_usd") or 0) + e["cost_usd"], 6)
            if e.get("verdict"):
                s["verdict"] = e["verdict"]
            if active == st:
                active = None
        elif ev == "run_end":
            ended = True
            end_phase = e.get("phase")
    # an ok-ended run paints its finished stages green ('complete')
    if ended and end_phase in ("DONE", "REVIEW", "READY"):
        for s in stages.values():
            if s.get("status") == "done":
                s["status"] = "complete"
    totals = {
        "dur_ms": sum(s.get("dur_ms", 0) for s in stages.values()),
        "tok_in": sum(s.get("tok_in", 0) for s in stages.values()),
        "tok_out": sum(s.get("tok_out", 0) for s in stages.values()),
        "cost_usd": round(sum(s.get("cost_usd", 0) for s in stages.values()), 6),
    }
    return {"run": last_run, "stages": stages, "totals": totals,
            "active": active, "ended": ended}


# --------------------------------------------------------------------------- #
# Board — tasks + per-task workflow assignment + launch/stop a background run
# --------------------------------------------------------------------------- #
def _context_reads_by_task(env_dir: Path) -> dict[str, list[dict]]:
    """{task_id: [{kind, name, ts}, …]} — what actually got pulled into context
    per task (read_skill/read_service/read_prd/read_guidance calls the agent
    itself made), vs. the task's `skills` field which is only a HINT of what's
    available. One pass over events.jsonl regardless of task count."""
    out: dict[str, list[dict]] = {}
    for e in events_mod.read(env_dir):
        if e.get("event") != "context_read":
            continue
        tid = e.get("task_id")
        if not tid:
            continue
        out.setdefault(tid, []).append(
            {"kind": e.get("kind"), "name": e.get("name"), "ts": e.get("ts")})
    return out


def board_payload(env_dir: Path) -> dict:
    """Every task (full detail: status, workflow, scratchpad, decisions,
    review_log, context actually loaded — how the board shows a task's context
    growing) + whether a UI-launched run is currently active, + its log tail."""
    reads = _context_reads_by_task(env_dir)
    ts = []
    for t in tasks_mod.load_tasks(env_dir):
        d = tasks_mod.to_dict(t)
        d["context_reads"] = reads.get(t.id, [])
        d["attachments"] = attachments_mod.list_files(env_dir, t.id)
        ts.append(d)
    run = runner_mod.active(env_dir)
    payload = {"tasks": ts, "run": run}
    if run:
        payload["run_log"] = runner_mod.log_tail(env_dir, 40)
    return payload


def blocked_question_payload(env_dir: Path, task_id: str) -> dict:
    """The pending BLOCKED question for this env, if any, so the Board tab
    can show it and let a human answer without leaving studio. The question
    text is the agent's own free-text prose (context, options, and its
    recommendation all embedded as written) — rendered verbatim, not parsed
    into buttons, since there is no separate structured options list
    anywhere in harn. `task_id` is accepted for route symmetry with the
    other per-task endpoints, but state is per-env (one active task at a
    time), so it's currently unused for the lookup itself."""
    st = state_mod.State.load(env_dir / "state")
    if st.phase == state_mod.BLOCKED and st.question:
        return {"question": st.question}
    return {"question": None}


def answer_payload(env_dir: Path, task_id: str, text: str) -> dict:
    """Answer a pending BLOCKED question from studio, reusing the SAME
    `loop.answer()` the `harn answer` CLI command calls — no BLOCKED-clearing
    logic is reimplemented here."""
    if not text.strip():
        return {"error": "answer text is empty"}
    from . import loop as loop_mod
    loop_mod.answer(env_dir, text, source="studio")
    return {"ok": True}


def set_task_workflow(env_dir: Path, payload: dict) -> dict:
    """Assign (or clear) a task's workflow preset. Empty -> project default."""
    task_id = (payload.get("task_id") or "").strip()
    t = tasks_mod.find(env_dir, task_id)
    if t is None:
        return {"ok": False, "error": f"no task {task_id}"}
    name = (payload.get("workflow") or "").strip()
    slug = workflows_mod._slug(name) if name else None
    if slug and not any(m["name"] == slug for m in workflows_mod.list_workflows(env_dir)):
        return {"ok": False, "error": f"unknown workflow '{name}'"}
    t.workflow = slug
    t.workflow_confirmed = True
    tasks_mod._save(t)
    return {"ok": True, "task_id": t.id, "workflow": t.workflow}


def create_task_payload(env_dir: Path, payload: dict) -> dict:
    """Create a new `todo` task from the board's "New task" form."""
    title = (payload.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    description = payload.get("description") or ""
    workflow = (payload.get("workflow") or "").strip() or None
    try:
        priority = int(payload.get("priority") or 10)
    except (TypeError, ValueError):
        priority = 10
    task = tasks_mod.create_task(env_dir, title, description=description,
                                 workflow=workflow, priority=priority)
    if workflow is not None:
        # Explicitly picking a named flow in the create form is itself the
        # "explicit action to set task.workflow" the in_progress gate wants
        # (see set_task_workflow's docstring) — don't make the user re-pick
        # the same flow in the detail panel just because they picked it here.
        task.workflow_confirmed = True
        tasks_mod._save(task)
    return {"ok": True, "task_id": task.id}


def set_task_status_payload(env_dir: Path, payload: dict) -> dict:
    """Move a task to a new lifecycle status (drag-drop on the board).

    Moving TO `in_progress` requires the task's flow to already be confirmed
    (`workflow_confirmed`, set by `set_task_workflow` — see its docstring) and
    atomically auto-launches a run for it via `runner_mod.launch`, the same
    call `launch_task` makes; if that launch refuses (e.g. another run is
    already active) the status change is rolled back so the task never ends
    up stuck at `in_progress` with nothing actually running. This route never
    touches `review_log` or calls `accept`/`request_changes` — it is a raw
    `tasks_mod.set_status` only.
    """
    task_id = (payload.get("task_id") or "").strip()
    new_status = (payload.get("status") or "").strip()
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    if new_status not in tasks_mod.LIFECYCLE:
        return {"ok": False, "error": f"unknown status: {new_status!r}"}
    active = runner_mod.active(env_dir)
    if active and active.get("task_id") == task_id:
        return {"ok": False, "error": "a run is active for this task — stop it first"}
    if new_status == tasks_mod.IN_PROGRESS:
        if not task.workflow_confirmed:
            return {"ok": False,
                    "error": "pick a flow for this task before starting it"}
        prior_status = task.status
        tasks_mod.set_status(task, new_status)
        result = runner_mod.launch(env_dir.parent, env_dir, task_id, auto=False)
        if not result.get("ok"):
            tasks_mod.set_status(task, prior_status)   # roll back — atomic with launch
            return {"ok": False, "error": result.get("error", "launch failed")}
        return {"ok": True, "task_id": task_id, "status": new_status,
                "launched": True}
    tasks_mod.set_status(task, new_status)
    return {"ok": True, "task_id": task_id, "status": new_status}


def task_plan_payload(env_dir: Path, task_id: str) -> dict:
    """The task's OWN workflow plan (harn_env/tasks/<id>.workflow.json) for the
    Board's "Edit this task's plan" button — the same Flow canvas component
    used for presets, just pointed at a per-task snapshot instead."""
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.preview_plan(env_dir, task_id, task.workflow)
    if plan is None:
        return {"ok": False, "error": "no plan"}
    return {"ok": True, "plan": plan, "task_id": task_id}


def step_prompt_payload(env_dir: Path, task_id: str, step_id: str) -> dict:
    """The EXACT prompt a step would receive (or did receive), with zero side
    effects — powers the Flow tab's "View full context" button. Reuses
    `task_plan_payload`'s own plan-lookup fallback (task's saved snapshot,
    else the preset's) and `launch_step`'s node-matching convention."""
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"error": f"no such task: {task_id}"}
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.preview_plan(env_dir, task_id, task.workflow)
    if plan is None:
        return {"error": "no plan"}
    nodes = plan.get("nodes", [])
    step = next((n for n in nodes if n.get("kind") == "step"
                and n.get("id") == step_id), None)
    if step is None:
        return {"error": f"no such step: {step_id}"}
    cfg = Config.load(env_dir)
    from . import loop as loop_mod
    text = loop_mod.preview_step_prompt(env_dir, cfg, task, step)
    return {"prompt": text}


def step_prompt_export_payload(env_dir: Path, task_id: str, step_id: str,
                                text: str) -> dict:
    """Write a previewed prompt to a plain text file the human can open or
    download directly — the "Copy to file" action on the preview panel."""
    if not text.strip():
        return {"error": "nothing to export"}
    from . import loop as loop_mod
    path = loop_mod.save_context_export(env_dir, task_id, step_id, text)
    return {"path": str(path), "name": path.name}


def save_task_plan_route(env_dir: Path, payload: dict) -> dict:
    """Persist edits made while a task's plan (not a preset) is open in the
    canvas — writes ONLY that task's snapshot file, never the preset. Echoes
    back the saved plan (ids now stamped) so the client can sync in place."""
    task_id = (payload.get("task_id") or "").strip()
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    plan = payload.get("plan") or {}
    workflows_mod.save_task_plan(env_dir, task_id, plan)
    return {"ok": True, "plan": plan}


def launch_task(env_dir: Path, payload: dict) -> dict:
    """Start a background `harn run --task <id>` for one task (see runner.py —
    single-runner-at-a-time; refuses if a run is already active)."""
    task_id = (payload.get("task_id") or "").strip()
    if tasks_mod.find(env_dir, task_id) is None:
        return {"ok": False, "error": f"no task {task_id}"}
    return runner_mod.launch(env_dir.parent, env_dir, task_id,
                             auto=bool(payload.get("auto")))


def stop_task(env_dir: Path, payload: dict) -> dict:
    """Stop the active UI-launched run (best-effort SIGTERM)."""
    return runner_mod.stop(env_dir)


def launch_step(env_dir: Path, payload: dict) -> dict:
    """Start (or rerun) exactly ONE workflow step for one task in the
    background — the Flow tab's per-step Run/Rerun controls. `rerun=True`
    first restores the working tree to that step's git checkpoint (see
    gitutil.checkpoint / loop.run_step), discarding its last attempt.

    A step that's part of a parallel wave (non-empty `parallel` field) needs a
    DIFFERENT undo than a normal step's checkpoint restore: that checkpoint is
    the whole wave's SHARED starting point (see `loop._run_parallel_wave`), so
    restoring straight to it would silently discard every sibling step's
    already-merged edit too. For a parallel step, Rerun instead calls
    `loop.rollback_parallel_step` HERE (synchronously, in the studio process,
    before the background rerun is launched) — it reverse-applies just this
    step's own saved patch, touching no sibling, and only falls back to
    reverting the whole wave if that's no longer possible. That fallback is
    never silent: its `note` is passed through in this route's response so the
    UI can surface it as an alert (see `rerunStep()` in the client script)."""
    task_id = (payload.get("task_id") or "").strip()
    step_id = (payload.get("step_id") or "").strip()
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.snapshot_for_task(env_dir, task_id, task.workflow)
    nodes = plan.get("nodes", [])
    step = next((n for n in nodes if n.get("kind") == "step"
                and n.get("id") == step_id), None)
    if step is None:
        return {"ok": False, "error": f"unknown step '{step_id}'"}
    rerun = bool(payload.get("rerun"))
    note = ""
    if rerun and (step.get("parallel") or "").strip():
        from . import loop as loop_mod
        rb = loop_mod.rollback_parallel_step(env_dir.parent, env_dir,
                                             task_id, step_id)
        if rb.get("mode") == "whole-wave-fallback":
            note = rb.get("note", "")
    result = runner_mod.launch(env_dir.parent, env_dir, task_id,
                               step=step_id, rerun=rerun)
    if note:
        result = {**result, "note": note}
    return result


def launch_stage(env_dir: Path, payload: dict) -> dict:
    """Deprecated: superseded by launch_step (per-step ids, not fixed
    pipeline stages). Kept only for the /api/tasks/run_stage interim route
    (see that route's handler) until the studio frontend (Task 6) is
    rewritten to post to /api/tasks/run_step."""
    return {"ok": False,
            "error": "replaced by /api/tasks/run_step (per-step ids)"}


def rerun_workflow(env_dir: Path, payload: dict) -> dict:
    """Rerun a task's WHOLE workflow from scratch: restore the working tree to
    its very first git baseline (undoing every stage's changes, not just one),
    reopen it to `todo` (clears scratchpad/decisions/stage checkpoints too —
    see loop.rollback's reopen=True), then launch the full loop again."""
    task_id = (payload.get("task_id") or "").strip()
    t = tasks_mod.find(env_dir, task_id)
    if t is None:
        return {"ok": False, "error": f"no task {task_id}"}
    if not t.baseline_ref:
        return {"ok": False, "error": "no baseline recorded yet — this task "
                "hasn't started its first attempt"}
    from . import loop as loop_mod
    res = loop_mod.rollback(env_dir.parent, env_dir, task_id, apply=True, reopen=True)
    if not res.ok:
        return {"ok": False, "error": res.message}
    return runner_mod.launch(env_dir.parent, env_dir, task_id,
                             auto=bool(payload.get("auto")))


# --------------------------------------------------------------------------- #
# Attachments — per-task files (design references, generated images, …)
# --------------------------------------------------------------------------- #
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024   # local admin tool; generous but not unbounded


def upload_attachment(env_dir: Path, payload: dict) -> dict:
    """Save a base64-encoded file to a task's attachments (from the Board's
    Upload button). Same storage the agent's save_attachment MCP tool writes
    to — either side can attach, both sides see everything."""
    task_id = (payload.get("task_id") or "").strip()
    filename = (payload.get("filename") or "").strip()
    if tasks_mod.find(env_dir, task_id) is None:
        return {"ok": False, "error": f"no task {task_id}"}
    if not filename:
        return {"ok": False, "error": "missing filename"}
    try:
        data = base64.b64decode(payload.get("content_b64") or "", validate=True)
    except Exception:
        return {"ok": False, "error": "content_b64 is not valid base64"}
    if len(data) > _MAX_ATTACHMENT_BYTES:
        mb = _MAX_ATTACHMENT_BYTES // (1024 * 1024)
        return {"ok": False, "error": f"file too large (max {mb}MB)"}
    p = attachments_mod.save(env_dir, task_id, filename, data)
    return {"ok": True, "name": p.name}


def delete_attachment(env_dir: Path, payload: dict) -> dict:
    """Remove one attachment from a task."""
    task_id = (payload.get("task_id") or "").strip()
    filename = (payload.get("filename") or "").strip()
    if not attachments_mod.delete(env_dir, task_id, filename):
        return {"ok": False, "error": "not found"}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
def _looks_like_env(p: Path) -> bool:
    """A directory we'll serve: a harn_env (has harn.toml or the expected subdirs)."""
    return p.is_dir() and ((p / "harn.toml").exists() or (p / "tasks").is_dir()
                           or (p / "skills").is_dir())


def _make_handler(default_env: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):  # quiet
            pass

        # The env for THIS request: from ?env=<abs path> (multi-project), else
        # the default the server was launched in. Validated so we don't serve
        # arbitrary folders.
        def _env(self):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            raw = (q.get("env") or [None])[0]
            if not raw:
                return default_env
            p = Path(raw).expanduser().resolve()
            return p if _looks_like_env(p) else None

        def _query(self, key: str) -> str:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            return (q.get(key) or [""])[0]

        def _send(self, code: int, body: bytes, ctype: str,
                  extra_headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _read_json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            route = urllib.parse.urlsplit(self.path).path
            if route in ("/", "/index", "/index.html"):
                html = _HTML.replace("__DEFAULT_ENV__", str(default_env))
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            env = self._env()
            if env is None:
                self._json({"error": "invalid or missing env"}, 400); return
            if route == "/api/state":
                self._json(state_payload(env))
            elif route == "/api/config":
                self._json(config_payload(env))
            elif route == "/api/progress":
                self._json(progress_payload(env))
            elif route == "/api/workflows":
                self._json(list_workflows_payload(env))
            elif route == "/api/tools":
                self._json(tools_catalog_payload(env))
            elif route == "/api/board":
                self._json(board_payload(env))
            elif route == "/api/tasks/blocked_question":
                self._json(blocked_question_payload(env, self._query("task") or ""))
            elif route == "/api/models":
                self._json(models_payload(env))
            elif route == "/api/task_plan":
                self._json(task_plan_payload(env, self._query("task") or ""))
            elif route == "/api/task_plan/step_prompt":
                task_id, step_id = self._query("task"), self._query("step")
                self._json(step_prompt_payload(env, task_id or "", step_id or ""))
            elif route == "/api/attachments/file":
                task_id, name = self._query("task"), self._query("name")
                data = attachments_mod.read_bytes(env, task_id, name)
                if data is None:
                    self._send(404, b"not found", "text/plain"); return
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self._send(200, data, ctype)
            elif route == "/api/tools/export":
                name = self._query("name") or ""
                tool = tools_mod.read(env, name)
                if tool is None:
                    self._send(404, b"not found", "text/plain"); return
                data = tools_mod.export_bundle(tool)
                is_zip = data[:2] == b"PK"
                fname = f"{name}.zip" if is_zip else f"{name}.json"
                ctype = "application/zip" if is_zip else "application/json"
                self._send(200, data, ctype, extra_headers={
                    "Content-Disposition": f'attachment; filename="{fname}"'})
            elif route == "/api/skill/export":
                name = self._query("name") or ""
                skill = next((s for s in skills_mod.discover(env) if s.name == name), None)
                if skill is None:
                    self._send(404, b"not found", "text/plain"); return
                data = skill.path.read_bytes()
                self._send(200, data, "text/markdown", extra_headers={
                    "Content-Disposition": f'attachment; filename="{name}.md"'})
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            route = urllib.parse.urlsplit(self.path).path
            env = self._env()
            if env is None:
                self._json({"error": "invalid or missing env"}, 400); return
            body = self._read_json()
            if route == "/api/workflow":
                self._json(apply_workflow(env, body))
            elif route == "/api/workflows/activate":
                self._json(activate_workflow(env, body))
            elif route == "/api/workflows/create":
                self._json(create_workflow(env, body))
            elif route == "/api/workflows/meta":
                self._json(save_workflow_meta(env, body))
            elif route == "/api/workflows/delete":
                self._json(delete_workflow(env, body))
            elif route == "/api/skill":
                self._json(apply_skill(env, body))
            elif route == "/api/skill/delete":
                self._json(delete_skill(env, body))
            elif route == "/api/tools/save":
                self._json(save_custom_tool_payload(env, body))
            elif route == "/api/tools/delete":
                self._json(delete_custom_tool_payload(env, body.get("name", "")))
            elif route == "/api/tools/import":
                self._json(import_custom_tool_bundle_payload(env, body))
            elif route == "/api/tools/chat":
                cfg = Config.load(env)
                self._json(draft_tool_chat_payload(env, env.parent, cfg, body))
            elif route == "/api/layout":
                self._json(apply_layout(env, body))
            elif route == "/api/config":
                self._json(set_config_flag(env, body))
            elif route == "/api/tasks/create":
                self._json(create_task_payload(env, body))
            elif route == "/api/tasks/status":
                self._json(set_task_status_payload(env, body))
            elif route == "/api/tasks/workflow":
                self._json(set_task_workflow(env, body))
            elif route == "/api/tasks/launch":
                self._json(launch_task(env, body))
            elif route == "/api/tasks/stop":
                self._json(stop_task(env, body))
            elif route == "/api/tasks/answer":
                self._json(answer_payload(env, body.get("task", ""), body.get("text", "")))
            elif route == "/api/tasks/run_stage":
                self._json(launch_stage(env, body))
            elif route == "/api/tasks/run_step":
                self._json(launch_step(env, body))
            elif route == "/api/tasks/rerun_workflow":
                self._json(rerun_workflow(env, body))
            elif route == "/api/attachments/upload":
                self._json(upload_attachment(env, body))
            elif route == "/api/attachments/delete":
                self._json(delete_attachment(env, body))
            elif route == "/api/task_plan":
                self._json(save_task_plan_route(env, body))
            elif route == "/api/defaults":
                self._json(save_defaults(env, body))
            elif route == "/api/task_plan/step_prompt/export":
                self._json(step_prompt_export_payload(
                    env, body.get("task", ""), body.get("step", ""),
                    body.get("text", "")))
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def serve(env_dir: Path, *, host: str = "127.0.0.1", port: int = 9999,
          open_browser: bool = True) -> None:
    """Block serving the studio until Ctrl-C. `env_dir` is the DEFAULT project;
    other projects open via ?env=<path> (multi-project, one server)."""
    workflow_mod.write(env_dir)  # ensure WORKFLOW.md exists
    httpd = ThreadingHTTPServer((host, port), _make_handler(env_dir))
    url = f"http://{host}:{port}"
    print(f"[harn] studio at {url}  (Ctrl-C to stop)")
    print(f"[harn] default project: {env_dir}  ·  open others with ?env=<path>")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[harn] studio stopped.")
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# The single-page app (vanilla JS, self-contained)
# --------------------------------------------------------------------------- #
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>harn studio</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1d212b; --line:#2a2f3a;
    --text:#e6e9ef; --muted:#9aa3b2; --accent:#7c8cff; --accent2:#3ad6a0;
    --chip:#262b36; --chipOn:#2b3a5e; --insp-w:400px;
    --danger:#ff6b6b; --warn:#e8b93a;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--text);height:100vh;overflow:hidden}
  header{display:flex;align-items:center;gap:14px;padding:10px 16px;
    background:var(--panel);border-bottom:1px solid var(--line)}
  header h1{font-size:15px;font-weight:600;margin:0;letter-spacing:.3px}
  header .dot{width:8px;height:8px;border-radius:50%;background:var(--accent2)}
  header .sp{flex:1}
  button{font:inherit;border:1px solid var(--line);background:var(--panel2);
    color:var(--text);padding:7px 12px;border-radius:8px;cursor:pointer}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#0b0d12;font-weight:600}
  .tabs{display:flex;gap:6px}
  .tabs button.active{border-color:var(--accent);color:var(--accent)}
  .wfbar{display:flex;align-items:center;gap:6px}
  .wfbar select{font:inherit;padding:6px 30px 6px 9px;max-width:200px;width:auto}
  .wfdesc{font-size:11px;color:var(--muted);max-width:200px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  button.ghost{padding:6px 8px;font-size:12px;line-height:1}
  /* one consistent minimal trash icon for every delete affordance in the app */
  .icon-btn{display:inline-flex;align-items:center;justify-content:center;
    width:30px;height:30px;padding:0;border:1px solid var(--line);
    background:var(--panel2);border-radius:8px;cursor:pointer;color:var(--muted)}
  .icon-btn svg{width:15px;height:15px}
  .icon-btn:hover{border-color:var(--danger);color:var(--danger)}
  .icon-btn:disabled{opacity:.35;cursor:not-allowed}
  .icon-btn:disabled:hover{border-color:var(--line);color:var(--muted)}
  .status.dirty{color:var(--warn);font-weight:600}
  .toolDoc{white-space:pre-wrap;font-size:12.5px;line-height:1.5;color:var(--text);
    background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px}
  /* ---- board tab ---- */
  .runbanner{display:flex;align-items:center;gap:10px;background:var(--panel2);
    border:1px solid var(--accent);border-radius:8px;padding:8px 10px;margin-bottom:12px;font-size:12.5px}
  .runbanner button{margin-left:auto}
  .blockedq{background:#3a2a10;border:1px solid var(--danger);padding:10px;
    border-radius:6px;margin-bottom:12px}
  .blockedq pre{white-space:pre-wrap;max-height:200px;overflow-y:auto;
    font:12.5px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:8px 0}
  .blockedq textarea{width:100%;margin-top:6px;background:var(--panel);color:var(--text);
    border:1px solid var(--line);border-radius:6px;padding:6px;font:inherit}
  /* ---- task-plan edit mode (Board -> Edit this task's plan) ---- */
  .planbanner{position:absolute;top:10px;left:50%;transform:translateX(-50%);z-index:20;
    display:flex;align-items:center;gap:10px;background:var(--panel2);
    border:1px solid var(--accent2);border-radius:8px;padding:7px 12px;font-size:12.5px}
  .planbanner button{margin-left:4px}
  .boardgroup{margin-bottom:16px}
  .bglabel{color:var(--muted);font-size:11px;letter-spacing:.6px;text-transform:uppercase;
    margin:0 0 6px;padding:0 6px}
  .taskrow.sel{background:var(--panel2);border-left:2px solid var(--accent)}
  .taskrow.running{border-left:2px solid var(--accent2)}
  .live-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent2);
    margin-left:6px;animation:blink 1s ease-in-out infinite}
  .statusbadge{font-size:11px;padding:3px 9px;border-radius:999px;border:1px solid;text-transform:uppercase;letter-spacing:.4px}
  .taskTitle{font-size:14px;font-weight:600;margin-bottom:4px}
  .pipeline{display:flex;flex-wrap:wrap;gap:6px}
  .pdot{font-size:11px;padding:4px 9px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
  .pdot.dot-pending{opacity:.5}
  .pdot.dot-done{border-color:#caa83a;color:#caa83a}
  .pdot.dot-complete{border-color:var(--accent2);color:var(--accent2)}
  .pdot.dot-active{border-color:var(--accent);color:var(--accent);animation:blink 1s ease-in-out infinite}
  .runlog{max-height:200px;overflow:auto;font-family:ui-monospace,Menlo,monospace;font-size:11.5px}
  .attgrid{display:flex;flex-wrap:wrap;gap:10px;margin-top:6px}
  .attcard{position:relative;width:96px;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;padding:6px;text-align:center}
  .attthumb{width:100%;height:64px;object-fit:cover;border-radius:6px;cursor:pointer;
    background:var(--panel);display:block}
  .attfile{display:flex;align-items:center;justify-content:center;font-size:28px}
  .attname{font-size:10.5px;color:var(--text);margin-top:5px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .attmeta{font-size:10px;color:var(--muted)}
  .attcard .attdel{position:absolute;top:3px;right:3px;width:20px;height:20px;
    background:rgba(15,17,21,.85);opacity:0;transition:opacity .15s}
  .attcard .attdel svg{width:11px;height:11px}
  .attcard:hover .attdel{opacity:1}
  .modelrow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
  .modelrow4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:6px}
  .modelrow input[type=text],.modelrow select,
  .modelrow4 input[type=text],.modelrow4 select{padding:6px 8px;font-size:12px}
  /* Settings panel (default agent + model for harn run) */
  .settings{max-width:560px}
  .settings .field{margin:14px 0}
  .settings label{display:block;margin-bottom:5px}
  .status{color:var(--muted);font-size:12px;min-width:120px;text-align:right}
  main{display:grid;grid-template-columns:1fr 6px var(--insp-w);height:calc(100vh - 53px)}
  .canvas{position:relative;overflow:auto;background:
    radial-gradient(circle at 1px 1px,#222732 1px,transparent 0) 0 0/24px 24px var(--bg)}
  .canvas.list{overflow:auto}
  .surface{position:relative;width:2000px;height:1500px;transform-origin:0 0}
  .listview{display:none;padding:18px 26px;max-width:760px}
  .listview h2{color:var(--muted);font-size:13px;letter-spacing:.6px;margin:0 0 10px}
  .zoom{position:fixed;right:calc(var(--insp-w) + 22px);bottom:16px;display:flex;
    align-items:center;gap:2px;background:var(--panel);border:1px solid var(--line);
    border-radius:8px;padding:2px;z-index:20;box-shadow:0 2px 10px rgba(0,0,0,.35)}
  .zoom button{padding:2px 9px;border:none;background:transparent}
  .zoom span{font-size:11px;color:var(--muted);min-width:42px;text-align:center;cursor:pointer}
  svg.edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
  .edge{fill:none;stroke:#3a4150;stroke-width:2}
  .node{position:absolute;width:260px;background:var(--panel);border:1px solid var(--line);
    border-radius:12px;padding:12px 14px;cursor:grab;user-select:none;z-index:1;
    box-shadow:0 2px 8px rgba(0,0,0,.25)}
  .node:hover{border-color:#3a4254}
  .node.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 4px 14px rgba(0,0,0,.35)}
  .node.drag{cursor:grabbing;z-index:5;opacity:.96}
  .node.off{opacity:.5;border-style:dashed}
  .node.off .ttl span:last-child{text-decoration:line-through}
  .nbtn{position:absolute;top:8px;right:8px;width:22px;height:22px;border-radius:6px;
    border:1px solid var(--line);background:var(--panel2);color:var(--muted);
    display:flex;align-items:center;justify-content:center;font-size:12px;cursor:pointer;z-index:2}
  .nbtn:hover{border-color:var(--accent);color:var(--text)}
  .nbtn.on{color:var(--accent2);border-color:#2f5a48}
  .node .ttl{font-weight:600;font-size:13.5px;display:flex;align-items:center;gap:8px;padding-right:24px}
  /* stepbtns (▶/↻) sit further left of the enable/disable toggle, right:34
     onward, ~48px wide — the title needs enough reserved padding to never sit
     under them, on every wrapped line, not just the first. */
  .node.has-run .ttl{padding-right:90px}
  .node .num{width:22px;height:22px;border-radius:6px;background:var(--panel2);
    border:1px solid var(--line);display:flex;align-items:center;justify-content:center;
    font-size:11px;color:var(--muted);flex:0 0 auto}
  .node .chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}
  .chip{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--chip);
    border:1px solid var(--line);color:var(--muted)}
  .chip.req{background:var(--chipOn);border-color:#3a4f7a;color:#cdd7f5}
  .chip.rec{border-style:dashed}
  .node .tools{margin-top:7px;font-size:11px;color:var(--muted);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .toolbadge{padding:1px 2px;border-bottom:2px solid transparent;border-radius:2px}
  /* Phase 4 Task 7: green/yellow/red usage badges on skill+tool chips, driven
     by task.step_results[sid]["usage"] (Task 6's post-step audit). Overrides
     the chip/tog/toolbadge base border-color regardless of how many other
     classes are already on the element (req/rec/on), hence !important. The
     "unused required" pulse reuses the same `blink` keyframes already defined
     for `.node.st-active` above rather than a new animation. */
  .badge-used{border-color:#2ecc71 !important}
  .badge-unused-recommended{border-color:#f1c40f !important}
  .badge-unused-required{border-color:#e74c3c !important;animation:blink 1.2s ease-in-out infinite}
  .grip{width:6px;cursor:col-resize;background:var(--line)}
  .grip:hover,.grip.act{background:var(--accent)}
  .insp{background:var(--panel);overflow:auto;padding:16px}
  .insp h2{font-size:13px;margin:0 0 4px;color:var(--muted);text-transform:uppercase;
    letter-spacing:.6px;font-weight:600}
  label{display:block;font-size:12px;color:var(--muted);margin:14px 0 5px}
  input[type=text],textarea,select{width:100%;background:var(--panel2);color:var(--text);
    border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit;
    appearance:none;-webkit-appearance:none}
  textarea{resize:vertical;min-height:90px;font-size:13px}
  select{cursor:pointer;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%239aa3b2' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>");
    background-repeat:no-repeat;background-position:right 10px center;background-size:14px;padding-right:32px}
  select:hover,select:focus{border-color:var(--accent);outline:none}
  .skillgrid{display:flex;flex-wrap:wrap;gap:6px}
  .tog{font-size:12px;padding:4px 9px;border-radius:8px;cursor:pointer;
    background:var(--chip);border:1px solid var(--line);color:var(--muted);user-select:none}
  .tog.on{background:var(--chipOn);border-color:#3a4f7a;color:#cdd7f5}
  .empty{color:var(--muted);padding:40px 10px;text-align:center}
  .skillrow{display:flex;align-items:center;gap:8px;padding:8px 6px;border-bottom:1px solid var(--line);cursor:pointer}
  .skillrow:hover{background:var(--panel2)}
  .skillrow .nm{font-weight:600;font-size:13px}
  .skillrow .ds{color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .row{display:flex;gap:8px;align-items:center}
  .mut{color:var(--muted);font-size:12px}
  a.link{color:var(--accent);cursor:pointer;text-decoration:none;font-size:12px}
  a.link:hover{text-decoration:underline}
  .hint{position:absolute;left:14px;bottom:10px;font-size:11px;color:var(--muted);
    background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:5px 9px;z-index:9}
  /* Notion-like rendered markdown */
  .md{font-size:13.5px;line-height:1.65;color:var(--text)}
  .md>:first-child{margin-top:0}
  .md h1,.md h2,.md h3,.md h4{font-weight:600;line-height:1.3;margin:16px 0 6px}
  .md h1{font-size:19px} .md h2{font-size:16px} .md h3{font-size:14px} .md h4{font-size:13px}
  .md p{margin:7px 0}
  .md ul,.md ol{margin:7px 0;padding-left:20px}
  .md li{margin:3px 0}
  .md code{background:var(--panel2);border:1px solid var(--line);border-radius:5px;
    padding:1px 5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  .md pre{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
    padding:11px 13px;overflow:auto;margin:9px 0}
  .md pre code{background:none;border:none;padding:0;font-size:12px}
  .md blockquote{border-left:3px solid var(--accent);margin:9px 0;padding:2px 0 2px 13px;color:var(--muted)}
  .md a{color:var(--accent)}
  .md hr{border:none;border-top:1px solid var(--line);margin:13px 0}
  .md strong{font-weight:600}
  .mdview{cursor:text;border-radius:8px;padding:9px 11px;border:1px solid transparent;min-height:42px}
  .mdview:hover{border-color:var(--line);background:var(--panel2)}
  .mdview.empty{color:var(--muted)}
  /* header project + toggles */
  .proj{font-size:11px;color:var(--muted);font-family:ui-monospace,Menlo,monospace;
    max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    border:1px solid var(--line);border-radius:6px;padding:3px 8px}
  .toggles{display:flex;gap:12px;align-items:center;margin-left:6px}
  .sw{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer;user-select:none}
  .sw input{display:none}
  .sw>span{width:30px;height:17px;border-radius:999px;background:var(--chip);
    border:1px solid var(--line);position:relative;transition:.15s}
  .sw>span::after{content:"";position:absolute;top:1px;left:1px;width:13px;height:13px;
    border-radius:50%;background:var(--muted);transition:.15s}
  .sw input:checked+span{background:var(--accent2);border-color:var(--accent2)}
  .sw input:checked+span::after{left:14px;background:#0b0d12}
  /* flow run animation: pending(grey) · active(blink blue) · done(yellow) · complete(green) */
  .node.st-pending{opacity:.6}
  .node.st-done{border-color:#caa83a;box-shadow:0 0 0 1px #caa83a55}
  .node.st-complete{border-color:var(--accent2);box-shadow:0 0 0 1px #3ad6a055}
  .node.st-active{border-color:var(--accent);animation:blink 1s ease-in-out infinite}
  @keyframes blink{0%,100%{box-shadow:0 0 0 1px var(--accent),0 0 0 0 #7c8cff00}
    50%{box-shadow:0 0 0 2px var(--accent),0 0 16px 2px #7c8cff88}}
  /* small inline spinner — used while an agent turn is in flight (e.g. tool-chat drafting) */
  .spinner{display:inline-block;width:13px;height:13px;border-radius:50%;
    border:2px solid var(--line);border-top-color:var(--accent);
    animation:spin .7s linear infinite;vertical-align:-2px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .thinking{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12px;padding:4px 0}
  .node .stat{margin-top:7px;font-size:10.5px;color:var(--muted);
    font-family:ui-monospace,Menlo,monospace;border-top:1px solid var(--line);padding-top:5px}
  /* Phase 3 "lane" — translucent backdrop grouping a wave of parallel steps.
     Non-interactive (pointer-events:none) and painted behind .node (z-index
     0 vs 1) so dragging/selecting/clicking members is completely unaffected. */
  .lane{position:absolute;z-index:0;pointer-events:none;border:1px dashed var(--accent);
    border-radius:14px;background:rgba(124,140,255,.07)}
  .lane-chip{position:absolute;top:5px;left:12px;font-size:10.5px;font-weight:600;
    color:var(--accent);background:var(--panel);border:1px solid var(--accent);
    border-radius:999px;padding:2px 9px;white-space:nowrap}
  /* terminal "Run workflow" block — the last node in the flow, not draggable */
  .node.terminal{cursor:default;width:280px;border-color:var(--accent);
    background:linear-gradient(180deg,var(--panel) 0%,var(--panel2) 100%)}
  .node.terminal:hover{border-color:var(--accent)}
  .node.terminal .kv{display:flex;justify-content:space-between;gap:14px;padding:2px 0;font-size:12px}
  .node.terminal .kv b{font-weight:500;font-family:ui-monospace,Menlo,monospace}
  .node.terminal .live{color:var(--accent2)}
  .node.terminal button{width:100%;margin-top:8px}
  .node.terminal select{margin-top:8px}
  /* per-step run/rerun controls, left of the existing enable/disable toggle */
  .stepbtns{position:absolute;top:8px;right:34px;display:flex;gap:4px;z-index:2}
  .stepbtn{width:22px;height:22px;border-radius:6px;border:1px solid var(--line);
    background:var(--panel2);color:var(--muted);display:flex;align-items:center;
    justify-content:center;font-size:11px;cursor:pointer}
  .stepbtn:hover{border-color:var(--accent);color:var(--text)}
  .stepbtn:disabled{opacity:.3;cursor:not-allowed}
  .stepbtn.rerun{color:var(--warn)}
</style>
</head>
<body>
<header>
  <span class="dot"></span><h1>harn studio</h1>
  <span class="proj" id="proj" title="current project (env)"></span>
  <div class="wfbar" id="wfbar" title="active workflow — the flow agents follow">
    <select id="wfSel" onchange="switchWorkflow(this.value)"></select>
    <span class="wfdesc" id="wfDesc"></span>
    <button class="ghost" onclick="newWorkflow()" title="New workflow preset">＋</button>
    <button class="ghost" onclick="editWorkflowMeta()" title="Edit name / description / version">✎</button>
    <button class="icon-btn" id="wfDelBtn" onclick="deleteWorkflow()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg></button>
  </div>
  <div class="tabs">
    <button id="tabFlow" class="active" onclick="showTab('flow')">Flow</button>
    <button id="tabSkills" onclick="showTab('skills')">Skills</button>
    <button id="tabTools" onclick="showTab('tools')">Tools</button>
    <button id="tabBoard" onclick="showTab('board')">Board</button>
    <button id="tabSettings" onclick="showTab('settings')">Settings</button>
  </div>
  <div class="toggles" id="toggles">
    <label class="sw"><input type="checkbox" id="tgSemble" onchange="setToggle('semble',this.checked)"><span></span>semble</label>
    <label class="sw"><input type="checkbox" id="tgSocratic" onchange="setToggle('socraticcode',this.checked)"><span></span>socraticode</label>
  </div>
  <span class="sp"></span>
  <span class="status" id="status">loading…</span>
  <button onclick="addStep()" id="addBtn">＋ Add step</button>
  <button onclick="addSkill()" id="addSkillBtn" style="display:none">＋ Add skill</button>
  <button onclick="autoArrange()" id="arrangeBtn">Auto-arrange</button>
  <button class="primary" id="saveBtn" onclick="saveFlow()">Save flow</button>
</header>
<main>
  <div class="canvas" id="canvas">
    <div class="planbanner" id="planBanner" style="display:none"></div>
    <div class="surface" id="surface">
      <svg class="edges" id="edges"></svg>
    </div>
    <div class="listview" id="listView"></div>
    <div class="hint" id="hint" style="display:none"></div>
    <div class="zoom" id="zoom"><button onclick="zoomBy(1/1.2)">−</button>
      <span id="zlbl" onclick="zoomReset()">100%</span>
      <button onclick="zoomBy(1.2)">＋</button></div>
  </div>
  <div class="grip" id="grip"></div>
  <div class="insp">
    <div id="blockedBanner" style="display:none"></div>
    <div id="insp"><div class="empty">Select a node to edit it.</div></div>
  </div>
</main>
<script>
const $=s=>document.querySelector(s);
// True when the user is mid-interaction with a form control inside `el`: a
// focused input/textarea, or an OPEN native <select> (which keeps itself as
// document.activeElement while its dropdown is showing). Poll-driven re-renders
// check this so a 1.5s tick never rebuilds the DOM out from under an open
// dropdown or a half-typed field.
function isEditing(el){
  const a=document.activeElement;
  return !!(a && el && el.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
}
// one consistent minimal trash icon, reused for every delete affordance.
const TRASH_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '+
  'stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline>'+
  '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>'+
  '<path d="M10 11v6"></path><path d="M14 11v6"></path>'+
  '<path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>';
let S={workflow:{preamble:"",nodes:[]},skills:[],layout:{},workflows:[],active:'default',toolDocs:{}};
let L={};                 // title -> {x,y}
let selNode=null, tab='flow', dirty=false, skillSel=-1, bodyMode='preview';
let PROG={stages:{},totals:{},active:null,ended:false};
// null = editing a workflow PRESET (the normal Flow tab). {taskId} = editing
// one task's OWN plan snapshot instead (opened via the Board's "Edit this
// task's plan" button) — same canvas component, different load/save target.
let PLAN_MODE=null;
let FLOW_SEL_TASK_ID=null;   // selected task id, survives DOM re-renders

/* multi-project: the env comes from ?env=<path>; a new tab with a different
   ?env opens another project against the same server. */
const DEFAULT_ENV="__DEFAULT_ENV__";
const ENV=new URLSearchParams(location.search).get('env')||DEFAULT_ENV;
if(!new URLSearchParams(location.search).get('env')){
  const u=new URL(location); u.searchParams.set('env',ENV); history.replaceState(null,'',u);
}
function api(p){ return p+(p.includes('?')?'&':'?')+'env='+encodeURIComponent(ENV); }

function setStatus(t){ $('#status').textContent=t; }
function markDirty(){ dirty=true; setStatus('unsaved changes'); $('#status').classList.add('dirty'); }
function clearDirty(){ dirty=false; $('#status').classList.remove('dirty'); }
// Dirty is TRUE CONTENT DIFF, not "something was clicked" — toggling a step off
// then back on (or any edit-then-undo) must NOT show "unsaved changes", since
// the file on disk would round-trip to exactly what's already saved.
let SAVED_SNAPSHOT=null;
function snapshotWorkflow(){ SAVED_SNAPSHOT=JSON.stringify(S.workflow); }
function checkDirty(){
  if(JSON.stringify(S.workflow)===SAVED_SNAPSHOT) clearDirty(); else markDirty();
}

let TOOL_DOCS={};   // name -> full MCP docstring; fetched once, static per install
let CUSTOM_TOOLS=[]; // [{name,description,params,command,source}]; refreshed alongside TOOL_DOCS
async function loadToolsData(force){
  if(!force && Object.keys(TOOL_DOCS).length) return;
  try{
    const r=await (await fetch(api('/api/tools'))).json();
    TOOL_DOCS=r.tools||{};
    CUSTOM_TOOLS=r.custom||[];
  }catch(e){}
}
async function load(){
  const r=await fetch(api('/api/state')); S=await r.json();
  L=Object.assign({}, S.layout||{});
  snapshotWorkflow(); clearDirty();
  setStatus(S.skills.length+' skills · '+S.workflow.nodes.filter(n=>n.kind==='step').length+' steps');
  await loadToolsData(false);
  try{ BOARD=await (await fetch(api('/api/board'))).json(); }catch(e){}
  await ensureModelsLoaded();
  renderWorkflows(); render(); loadConfig(); pollProgress();
  setInterval(()=>{ pollProgress(); pollBoard(); }, 1500);
}
function toolDoc(name){ return TOOL_DOCS[name] || '(custom / external tool — not a registered harn MCP tool)'; }
/* ---------- workflow switcher (named presets) ---------- */
function renderWorkflows(){
  const sel=$('#wfSel'); if(!sel) return;
  const list=S.workflows||[]; const active=S.active||'default';
  sel.innerHTML='';
  list.forEach(w=>{ const o=document.createElement('option');
    o.value=w.name; o.textContent=w.title+(w.version?(' · v'+w.version):'');
    if(w.name===active)o.selected=true; sel.appendChild(o); });
  const cur=list.find(w=>w.name===active)||{};
  $('#wfDesc').textContent=cur.description||'';
  $('#wfDesc').title=cur.description||'';
  const del=$('#wfDelBtn'); const isDefault=active==='default';
  del.disabled=isDefault;
  del.title=isDefault?"The default workflow can't be deleted.":'Delete this preset';
}
function activeWf(){ return (S.workflows||[]).find(w=>w.name===(S.active||'default'))||{}; }
async function switchWorkflow(name){
  if(dirty && !confirm('Unsaved flow edits will be lost. Switch workflow anyway?')){
    renderWorkflows(); return; }
  setStatus('switching…');
  await fetch(api('/api/workflows/activate'),{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  clearDirty(); await load();
}
async function newWorkflow(){
  const name=prompt('New workflow name (a-z, 0-9, -):'); if(!name)return;
  const title=prompt('Title:',name)||name;
  const description=prompt('Description (what this workflow is for):','')||'';
  const version=prompt('Version:','1')||'1';
  setStatus('creating…');
  const r=await fetch(api('/api/workflows/create'),{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,title,description,version})});
  const j=await r.json();
  if(!j.ok){ setStatus('create failed'); alert(j.error||'failed'); return; }
  clearDirty(); await load();
}
async function editWorkflowMeta(){
  const w=activeWf();
  const title=prompt('Title:',w.title||''); if(title===null)return;
  const description=prompt('Description:',w.description||''); if(description===null)return;
  const version=prompt('Version:',w.version||'1'); if(version===null)return;
  let new_name=w.name;
  if(w.name!=='default'){ const nn=prompt('Rename (slug, blank = keep):',w.name);
    if(nn===null)return; new_name=nn.trim()||w.name; }
  setStatus('saving…');
  const r=await fetch(api('/api/workflows/meta'),{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:w.name,title,description,version,new_name})});
  const j=await r.json();
  if(!j.ok){ setStatus('save failed'); alert(j.error||'failed'); return; }
  await load();
}
async function deleteWorkflow(){
  const w=activeWf();
  if(w.name==='default'){ alert("The default workflow can't be deleted."); return; }
  if(!confirm('Delete workflow "'+w.title+'"? Tasks using it fall back to the default.'))return;
  await fetch(api('/api/workflows/delete'),{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name:w.name})});
  clearDirty(); await load();
}
/* ---------- header: project + code-search toggles ---------- */
async function loadConfig(){
  const c=await (await fetch(api('/api/config'))).json();
  $('#proj').textContent=c.project? c.project+' · '+shortEnv(c.env) : shortEnv(c.env);
  $('#tgSemble').checked=!!(c.toggles&&c.toggles.semble);
  $('#tgSocratic').checked=!!(c.toggles&&c.toggles.socraticcode);
}
function shortEnv(p){ p=p||ENV; const parts=p.split('/'); return parts.slice(-2).join('/'); }
async function setToggle(key,val){
  await fetch(api('/api/config'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key,value:val})});
}
/* ---------- live run animation (events.jsonl) ---------- */
let pollPrevRun=null;   // track run state transitions across poll cycles
async function pollProgress(){
  try{ PROG=await (await fetch(api('/api/progress'))).json(); }catch(e){ return; }
  if(tab==='flow'){
    applyProgress();
    // Re-render the terminal block when: actively running (live status),
    // or the run state just transitioned (started/finished).  When idle
    // AND stable, skip — replacing innerHTML every 1.5s would destroy
    // the <select> mid-interaction and prevent the native dropdown from
    // opening.
    const term=document.querySelector('.node.terminal');
    if(term && (BOARD.run || !!BOARD.run !== !!pollPrevRun)){
      renderFlowTerminal(term);
    }
    pollPrevRun=BOARD.run;
  }
}

/* ---------- board tab: tasks + per-task workflow + launch/observe a run ---------- */
let BOARD={tasks:[],run:null,run_log:''}, boardSel=null;
const BOARD_ORDER=['todo','in_progress','review','changes_requested','done'];
const BOARD_LABEL={todo:'To do',in_progress:'In progress',review:'Awaiting your review',
  changes_requested:'Changes requested',done:'Done'};

async function pollBoard(){
  try{ BOARD=await (await fetch(api('/api/board'))).json(); }catch(e){ return; }
  if(tab!=='board') return;
  // Don't rebuild the list out from under an open "＋ New task" form (even
  // before the user has focused a field in it) or a half-typed value inside
  // it — same guard pattern as the inspector re-render below.
  if(!NEW_TASK_OPEN && !isEditing($('#listView'))) renderBoard();
  if(boardSel&&(BOARD.tasks||[]).some(t=>t.id===boardSel)){
    // Don't rebuild the inspector out from under an open <select> or a focused
    // input on this 1.5s tick — it would snap a dropdown shut mid-choice or
    // steal focus mid-typing. The next tick refreshes once the user is done.
    if(!isEditing($('#insp'))) renderTaskDetail();
    await pollBlockedQuestion();
  } else{
    boardSel=null; $('#insp').innerHTML='<div class="empty">Select a task.</div>';
    BLOCKED_Q_TASK=null; BLOCKED_Q_TEXT=null;
    const el=$('#blockedBanner'); if(el){ el.style.display='none'; el.innerHTML=''; }
  }
}
let BLOCKED_Q_TASK=null, BLOCKED_Q_TEXT=null;
async function pollBlockedQuestion(){
  if(!boardSel) return;
  const el=$('#blockedBanner');
  if(!el) return;
  let r;
  try{ r=await (await fetch(api(`/api/tasks/blocked_question?task=${encodeURIComponent(boardSel)}`))).json(); }
  catch(e){ return; }
  // Re-render ONLY when the question (or selected task) actually changed —
  // this poll fires every 1.5s, and blindly overwriting the banner's innerHTML
  // every tick would wipe out whatever the human is mid-typing in the answer
  // box before they get a chance to hit Submit.
  if(r.question){
    if(BLOCKED_Q_TASK===boardSel && BLOCKED_Q_TEXT===r.question && el.style.display==='block') return;
    BLOCKED_Q_TASK=boardSel; BLOCKED_Q_TEXT=r.question;
    el.style.display='block';
    el.innerHTML=`<div class="blockedq"><b>Blocked — needs your answer:</b>`+
      `<pre>${esc(r.question)}</pre>`+
      `<textarea id="answerBox" rows="3" placeholder="Your answer..."></textarea>`+
      `<button onclick="submitAnswer()">Submit answer</button></div>`;
  } else {
    BLOCKED_Q_TASK=null; BLOCKED_Q_TEXT=null;
    el.style.display='none'; el.innerHTML='';
  }
}
async function submitAnswer(){
  const box=$('#answerBox');
  const text=box?box.value:'';
  if(!text.trim()){ alert('Enter an answer first.'); return; }
  const r=await post_('/api/tasks/answer',{task:boardSel, text});
  if(r.error){ alert(r.error); return; }
  const el=$('#blockedBanner'); if(el){ el.style.display='none'; el.innerHTML=''; }
  await pollBoard();
}
function selectTask(id){
  boardSel=id; BLOCKED_Q_TASK=null; BLOCKED_Q_TEXT=null;
  renderBoard(); renderTaskDetail(); pollBlockedQuestion();
}
// Tracks whether the "＋ New task" form is open, independent of DOM focus —
// a click on "＋ New task" or "Create"/"Cancel" doesn't leave an INPUT/TEXTAREA/
// SELECT focused, so isEditing() alone can't stop the 1.5s poll from wiping the
// form back to hidden the instant the user opens it before typing anything.
let NEW_TASK_OPEN=false;
function renderBoard(){
  const v=$('#listView');
  const groups={}; (BOARD.tasks||[]).forEach(t=>(groups[t.status]=groups[t.status]||[]).push(t));
  let html='<h2>BOARD</h2>'+
    '<button class="ghost" onclick="showNewTaskForm()" style="margin-bottom:8px">＋ New task</button>'+
    `<div id="newTaskForm" style="display:${NEW_TASK_OPEN?'block':'none'}"></div>`;
  if(BOARD.run){
    const rt=(BOARD.tasks||[]).find(t=>t.id===BOARD.run.task_id);
    html+=`<div class="runbanner">▶ running <b>${esc(BOARD.run.task_id)}</b>`+
      `${rt?': '+esc(rt.title):''} (pid ${BOARD.run.pid}${BOARD.run.auto?' · auto':''})`+
      `<button class="ghost" onclick="stopRun()" title="Steps already done stay done; edit the plan, then ▶ Resume">⏸ Pause</button></div>`;
  }
  BOARD_ORDER.forEach(s=>{
    const list=(groups[s]||[]).slice().sort((a,b)=>a.priority-b.priority);
    if(!list.length) return;
    html+=`<div class="boardgroup"><div class="bglabel">${BOARD_LABEL[s]||s} · ${list.length}</div>`;
    list.forEach(t=>{
      const running=BOARD.run&&BOARD.run.task_id===t.id;
      html+=`<div class="skillrow taskrow ${boardSel===t.id?'sel':''} ${running?'running':''}" onclick="selectTask('${esc(t.id)}')">`+
        `<div style="width:100%"><div class="nm">${esc(t.id)}: ${esc(t.title)}${running?'<span class="live-dot" title="running"></span>':''}</div>`+
        `<div class="ds">${esc(t.workflow||'default workflow')} · priority ${t.priority}${t.claimed_by?' · '+esc(t.claimed_by):''}</div></div></div>`;
    });
    html+='</div>';
  });
  if(!(BOARD.tasks||[]).length) html+='<div class="empty">No tasks yet — create one from an agent session (create_task).</div>';
  v.innerHTML=html;
}
function showNewTaskForm(){
  NEW_TASK_OPEN=true;
  const wfOpts=(S.workflows||[]).map(w=>`<option value="${esc(w.name)}">${esc(w.title)}</option>`).join('');
  const el=$('#newTaskForm');
  el.style.display='block';
  el.innerHTML=`
    <div class="skillrow" style="flex-direction:column;align-items:stretch;gap:6px">
      <input type="text" id="ntTitle" placeholder="Title"/>
      <textarea id="ntDesc" placeholder="Description (optional)" rows="2"></textarea>
      <select id="ntWorkflow"><option value="">default workflow</option>${wfOpts}</select>
      <input type="number" id="ntPriority" placeholder="Priority (default 10)" value="10"/>
      <div class="row" style="gap:6px">
        <button class="primary" onclick="submitNewTask()">Create</button>
        <button class="ghost" onclick="closeNewTaskForm()">Cancel</button>
      </div>
    </div>`;
}
function closeNewTaskForm(){
  NEW_TASK_OPEN=false;
  $('#newTaskForm').style.display='none';
}
async function submitNewTask(){
  const title=$('#ntTitle').value.trim();
  if(!title){ alert('Title is required.'); return; }
  const r=await post_('/api/tasks/create',{
    title, description:$('#ntDesc').value, workflow:$('#ntWorkflow').value,
    priority:$('#ntPriority').value});
  if(!r.ok){ alert(r.error||'create failed'); return; }
  closeNewTaskForm();
  await pollBoard();
  selectTask(r.task_id);
}
function renderPipelineDots(t){
  const running=BOARD.run&&BOARD.run.task_id===t.id;
  if(!running) return '<span class="mut">not running — Launch to see live stages</span>';
  // Every step id seen so far in this run's events (PROG.stages is keyed by
  // step id, not a fixed pipeline name — steps are now arbitrary per-task).
  const ids=Object.keys(PROG.stages||{});
  if(!ids.length) return '<span class="mut">starting…</span>';
  return ids.map(id=>{
    const info=(PROG.stages||{})[id];
    const cls=info?(info.status==='active'?'dot-active':info.status==='complete'?'dot-complete':'dot-done'):'dot-pending';
    return `<span class="pdot ${cls}">${esc(id)}</span>`;
  }).join('');
}
function renderTaskDetail(){
  const t=(BOARD.tasks||[]).find(x=>x.id===boardSel);
  if(!t){ $('#insp').innerHTML='<div class="empty">Select a task.</div>'; return; }
  const running=BOARD.run&&BOARD.run.task_id===t.id;
  const busy=!!BOARD.run;   // some run (maybe a different task) is active
  const wfOpts=(S.workflows||[]).map(w=>
    `<option value="${esc(w.name)}" ${(t.workflow||'default')===w.name?'selected':''}>${esc(w.title)}</option>`).join('');
  const statusColor={todo:'var(--muted)',in_progress:'var(--accent)',review:'var(--accent2)',
    changes_requested:'var(--warn)',done:'var(--accent2)'}[t.status]||'var(--muted)';
  const reviewLog=(t.review_log||[]).map(e=>
    `${e.ts||''} ${e.event}${e.by?' by '+e.by:e.agent?' ('+e.agent+')':''}${e.summary?': '+e.summary:''}${e.comment?': '+e.comment:''}${e.notes?' — '+e.notes:''}`
  ).join('\n') || '(none yet)';
  const decisions=(t.decisions||[]).map(d=>
    `<span class="chip" title="${esc(d.rationale||'')}">${esc(d.decision)}</span>`).join('') || '<span class="mut">none yet</span>';
  const attHtml=(t.attachments||[]).length
    ? `<div class="attgrid">${t.attachments.map(a=>{
        const url=api('/api/attachments/file?task='+encodeURIComponent(t.id)+'&name='+encodeURIComponent(a.name));
        const thumb=a.kind==='image'
          ? `<img src="${url}" class="attthumb" onclick="window.open('${url}','_blank')" title="Open full size"/>`
          : `<div class="attthumb attfile" onclick="window.open('${url}','_blank')" title="Download">📄</div>`;
        return `<div class="attcard">${thumb}
          <div class="attname" title="${esc(a.name)}">${esc(a.name)}</div>
          <div class="attmeta">${fmtBytes(a.size)}</div>
          <button class="icon-btn attdel" onclick="deleteAttachment('${esc(t.id)}','${esc(a.name)}')" title="Delete">${TRASH_SVG}</button>
        </div>`;
      }).join('')}</div>`
    : '<span class="mut">no files attached — drop a design reference, screenshot, or anything the agent should see</span>';
  const KIND_LABEL={skill:'Skills',service:'Services',prd:'PRDs',guidance:'Guidance'};
  const readsByKind={};
  (t.context_reads||[]).forEach(r=>{ (readsByKind[r.kind]=readsByKind[r.kind]||[]).push(r); });
  const contextHtml=Object.keys(readsByKind).length
    ? Object.entries(readsByKind).map(([kind,items])=>
        `<div style="margin-bottom:6px"><span class="mut" style="font-size:11px">${esc(KIND_LABEL[kind]||kind)}:</span> `+
        items.map(r=>`<span class="chip" title="loaded ${esc(r.ts||'')}">${esc(r.name)}</span>`).join(' ')+`</div>`
      ).join('')
    : '<span class="mut">nothing pulled into context yet — skill NAMES are always in the prompt, but a body only enters context when the agent calls read_skill/read_service/read_prd/read_guidance</span>';
  $('#insp').innerHTML=`
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">${esc(t.id)}</h2>
      <select onchange="changeTaskStatus('${esc(t.id)}',this.value)" style="color:${statusColor};border-color:${statusColor};background:transparent">
        ${['todo','in_progress','review','changes_requested','done'].map(s=>
          `<option value="${s}" ${t.status===s?'selected':''}>${esc(s)}</option>`).join('')}
      </select>
    </div>
    <div class="taskTitle">${esc(t.title)}</div>
    <label>Workflow <span class="mut">(what the agent follows when this task runs)</span></label>
    <select onchange="assignWorkflow('${esc(t.id)}',this.value)">${wfOpts}</select>
    <div class="row" style="margin-top:8px">
      <button class="ghost" onclick="openTaskPlan('${esc(t.id)}')" title="Open this task's own copy of its plan — edits affect only this task">✎ Edit this task's plan</button>
      <button class="ghost" onclick="FLOW_SEL_TASK_ID='${esc(t.id)}';showTab('flow')" title="Watch this task's flow execute">Open flow ▶</button>
    </div>
    <div class="row" style="margin-top:12px;gap:8px">
      ${running
        ? `<button class="primary" onclick="stopRun()" style="background:var(--danger);border-color:var(--danger)">■ Stop</button>`
        : `<button class="primary" onclick="launchTask('${esc(t.id)}',false)" ${busy?'disabled':''} title="${busy?'Another run is active':'Run this task under its workflow'}">▶ Launch</button>
           <button onclick="launchTask('${esc(t.id)}',true)" ${busy?'disabled':''} title="Autonomous: no human-in-the-loop, harn_env .md files untouched">▶ Launch (auto)</button>`}
    </div>
    <label style="margin-top:14px">Description</label>
    <div class="toolDoc">${esc(t.description||'(none)')}</div>
    <label style="display:flex;justify-content:space-between;align-items:center">
      <span>Attachments <span class="mut">(design references, screenshots — visible to the agent too)</span></span>
      <button class="ghost" onclick="pickAttachment('${esc(t.id)}')" style="padding:3px 9px;font-size:11px">＋ Upload</button>
    </label>
    ${attHtml}
    <input type="file" id="attInput" style="display:none" onchange="uploadPickedFile('${esc(t.id)}',this)"/>
    <label>Pipeline <span class="mut">(live while running)</span></label>
    <div class="pipeline">${renderPipelineDots(t)}</div>
    <label>Context loaded <span class="mut">(what actually entered the agent's context — not just what was available)</span></label>
    <div class="toolDoc">${contextHtml}</div>
    <label>Context growing <span class="mut">(scratchpad the agent carries forward)</span></label>
    <div class="toolDoc">${esc(t.scratchpad||'(empty)')}</div>
    <label>Decisions <span class="mut">(claims the oracle verifies)</span></label>
    <div class="skillgrid">${decisions}</div>
    <label>Review log</label>
    <div class="toolDoc" style="max-height:160px;overflow:auto">${esc(reviewLog)}</div>
    ${running?`<label>Run log <span class="mut">(live stdout/stderr tail)</span></label><div class="toolDoc runlog">${esc(BOARD.run_log||'(starting…)')}</div>`:''}
  `;
}
async function assignWorkflow(taskId,name){
  await fetch(api('/api/tasks/workflow'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({task_id:taskId,workflow:name==='default'?'':name})});
  await pollBoard();
}
async function changeTaskStatus(taskId,status){
  const r=await post_('/api/tasks/status',{task_id:taskId,status});
  if(!r.ok){ alert(r.error||'status change failed'); await pollBoard(); return; }
  await pollBoard();
}
async function launchTask(taskId,auto){
  if(!confirm((auto?'Launch (autonomous) ':'Launch ')+taskId+' now? An agent will start making changes in the background.'))return;
  const r=await post_('/api/tasks/launch',{task_id:taskId,auto});
  if(!r.ok){ alert(r.error||'launch failed'); return; }
  await pollBoard();
}
async function stopRun(){
  if(!confirm('Stop the active run? The task stays where it is and can be resumed later.'))return;
  await post_('/api/tasks/stop',{});
  await pollBoard();
}
async function post_(p,b){
  const r=await fetch(api(p),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
  return r.json();
}
/* ---------- per-step agent + model (for harn run) ---------- */
// Every step now carries its OWN agent/model/effort/temperature fields
// directly (see harn/workflow.py) — no more stage-keyed [models.<stage>]
// indirection. `default_agent`/`default_model` are the [harn] settings
// edited in Settings, used when a step leaves its fields blank.
let MODELS={agent_chain:[],agents:{},default_agent:'',default_model:''};
const AGENT_LABEL={claude:'Claude Code',codex:'Codex',cursor:'Cursor',
  qwen:'Qwen',antigravity:'Antigravity'};
async function ensureModelsLoaded(){
  if(Object.keys(MODELS.agents||{}).length) return;
  try{ MODELS=await (await fetch(api('/api/models'))).json(); }catch(e){}
}
function allAgentNames(){ return Object.keys(MODELS.agents||{}); }
function agentCaps(name){ return (MODELS.agents||{})[name]||{}; }
// The choices for THIS step's model/effort/temperature dropdowns, driven by
// whichever agent the step is set to run under (else the [harn] default).
function stepChoices(n){
  const c=agentCaps(n.agent||MODELS.default_agent||'');
  return {models:c.models||[], efforts:c.efforts||[], temperatures:c.temperatures||[]};
}
// Direct setter for a step's agent/model/effort/temperature field — replaces
// the old stage-keyed setModelField/setStageAgent/saveStepModel plumbing.
// Changing the agent can invalidate the current model, so it's cleared and
// the inspector re-rendered (same guard the old setStageAgent had).
function setStepField(field,val,rerender){
  if(!selNode) return;
  selNode[field]=(val||'').trim();
  if(field==='agent'){
    const valid=(agentCaps(selNode.agent||MODELS.default_agent||'').models)||[];
    if(selNode.model && valid.length && !valid.includes(selNode.model)) selNode.model='';
  }
  checkDirty();
  // Only structural changes (a <select> that swaps which fields the inspector
  // shows, or that changes another field's option list) need a re-render.
  // Free-text <input>/<textarea> handlers pass rerender=false: re-rendering on
  // every keystroke rebuilds the very element being typed into and steals its
  // focus after each character.
  if(rerender!==false) renderInsp();
}
// A <select> of known values + a "Custom…" escape hatch (curated lists go
// stale as providers ship new models — this keeps typing-it-yourself always
// possible instead of hard-blocking on the list). `keyid` is a DOM-safe id
// (the step's own id) so multiple steps' custom inputs don't collide.
function selectOrCustom(keyid,field,current,options){
  const known=options.includes(current);
  const isCustom=!!current && !known;
  const sel=`<select onchange="onModelSelect('${esc(keyid)}','${field}',this)">
    <option value="">(none)</option>
    ${options.map(o=>`<option value="${esc(o)}" ${current===o?'selected':''}>${esc(o)}</option>`).join('')}
    <option value="__custom__" ${isCustom?'selected':''}>Custom…</option>
  </select>
  <input type="text" placeholder="custom ${esc(field)}" value="${esc(isCustom?current:'')}"
    style="margin-top:4px;${isCustom?'':'display:none'}" id="mc-${esc(keyid)}-${field}"
    oninput="setStepField('${field}',this.value,false)"/>`;
  return sel;
}
function onModelSelect(keyid,field,sel){
  const inp=$('#mc-'+keyid+'-'+field);
  if(sel.value==='__custom__'){
    if(inp){ inp.style.display=''; inp.value=''; inp.focus(); }
    setStepField(field,'');
  }else{
    if(inp) inp.style.display='none';
    setStepField(field,sel.value);
  }
}
/* ---------- attachments: upload / delete (design refs, screenshots, …) ---------- */
function pickAttachment(taskId){ $('#attInput').click(); }
async function uploadPickedFile(taskId,input){
  const file=input.files&&input.files[0]; if(!file)return;
  const dataUrl=await new Promise((res,rej)=>{
    const r=new FileReader(); r.onload=()=>res(r.result); r.onerror=rej; r.readAsDataURL(file);
  });
  const content_b64=dataUrl.split(',')[1]||'';
  const r=await post_('/api/attachments/upload',{task_id:taskId,filename:file.name,content_b64});
  if(!r.ok){ alert(r.error||'upload failed'); return; }
  input.value='';
  await pollBoard();
}
async function deleteAttachment(taskId,name){
  if(!confirm('Delete "'+name+'"?'))return;
  await post_('/api/attachments/delete',{task_id:taskId,filename:name});
  await pollBoard();
}
function fmtDur(ms){ if(!ms) return '0s'; const s=Math.round(ms/1000); return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s'; }
function fmtBytes(n){ if(!n) return '0B'; if(n<1024) return n+'B'; if(n<1048576) return (n/1024).toFixed(1)+'KB'; return (n/1048576).toFixed(1)+'MB'; }
function hasRun(){ return PROG.run && Object.keys(PROG.stages||{}).length>0; }
// The selected task's `step_results` entry for a step — the same data
// `run_step()`/the engine write for BOTH agent and command steps, but only
// command steps have no dur_ms/tok_in of their own to show, so their canvas
// stat line and inspector output block read this directly instead.
function selTaskStepResult(stepId){
  const taskId=runStepTaskId();
  const t=taskId&&(BOARD.tasks||[]).find(x=>x.id===taskId);
  return (t&&t.step_results&&t.step_results[stepId])||null;
}
// Phase 4 Task 7: green/yellow/red usage badge for one skill/tool chip, from
// the same step's `usage` audit Task 6 writes into step_results (only present
// once the step has actually run — a step with no run yet, or no id at all,
// just gets no badge class, same as `selTaskStepResult` returning null above).
function usageBadgeClass(stepId, kind, name){
  const res=stepId&&selTaskStepResult(stepId);
  const usage=res&&res.usage&&res.usage[kind];
  const tier=usage&&usage[name];
  if(tier==='used') return 'badge-used';
  if(tier==='unused_required') return 'badge-unused-required';
  if(tier==='unused_recommended') return 'badge-unused-recommended';
  return '';
}
function lastRunOutputBlock(n){
  const res=n.id&&selTaskStepResult(n.id);
  if(!res) return `<label>Last run output</label><div class="toolDoc runlog" id="cmdOutput">(not run yet)</div>`;
  const mark=res.status==='ok'?'✓ ok':res.status==='failed'?'✗ failed':res.status||'…';
  return `<label>Last run output <span class="mut">(${esc(mark)})</span></label>
    <div class="toolDoc runlog" id="cmdOutput">${esc(res.output||'(no output)')}</div>`;
}
function applyProgress(){
  const st=PROG.stages||{};
  const live=hasRun();   // only animate when a run actually has stages
  document.querySelectorAll('.node').forEach(el=>{
    el.classList.remove('st-active','st-done','st-complete','st-pending');
    if(!live) return;
    const n=S.workflow.nodes[+el.dataset.i]; if(!n||n.kind!=='step') return;
    const info=n.id&&st[n.id];
    let cls='st-pending';
    if(info){ cls = info.status==='active'?'st-active': info.status==='complete'?'st-complete':'st-done'; }
    el.classList.add(cls);
    let line=el.querySelector('.stat');
    if(n.type==='command'){
      const res=n.id&&selTaskStepResult(n.id);
      if(info&&res){
        const lastLine=(res.output||'').trim().split('\n').filter(Boolean).pop()||'';
        const mark=res.status==='ok'?'✓ ok':res.status==='failed'?'✗ failed':res.status||'…';
        if(!line){ line=document.createElement('div'); line.className='stat'; el.appendChild(line); }
        line.textContent=lastLine?`${mark} · ${lastLine.slice(0,60)}`:mark;
      } else if(line){ line.remove(); }
      return;
    }
    if(info&&(info.dur_ms||info.tok_in||info.tok_out)){
      const tok=(info.tok_in||0)+(info.tok_out||0);
      const cost=info.cost_usd?` · $${info.cost_usd.toFixed(4)}`:'';
      if(!line){ line=document.createElement('div'); line.className='stat'; el.appendChild(line); }
      line.textContent=`${fmtDur(info.dur_ms)} · ${tok} tok${cost}`;
    } else if(line){ line.remove(); }
  });
  // Targeted live-refresh of an OPEN command step's "Last run output" box —
  // never a full renderInsp() here, so editing the Command textarea doesn't
  // get its cursor/focus stolen by a poll tick landing mid-keystroke.
  const outEl=document.getElementById('cmdOutput');
  if(outEl && selNode && selNode.type==='command'){
    const res=selNode.id&&selTaskStepResult(selNode.id);
    outEl.textContent = res ? (res.output||'(no output)') : '(not run yet)';
  }
}
// `harn run` (the whole workflow) only picks up tasks in these statuses
// (tasks.next_task's needs_agent) — 'review'/'done' won't move further.
// Per-step Run/Rerun has NO such restriction: forcing one specific step to
// run again is a valid action regardless of where the task currently sits
// (e.g. rerun 'verify' on a task already in review, before approving it).
const RUNNABLE_STATUSES=['todo','in_progress','changes_requested'];

function flowTaskSel(){ return $('#flowTaskSel'); }
// The picker lists EVERY task (not just runnable ones) so per-step Run/Rerun
// stays reachable after a task moves to review/done.
function flowAllTasks(){ return BOARD.tasks||[]; }
function flowSelectedTaskId(){
  const sel=flowTaskSel();
  if(sel&&sel.value&&flowAllTasks().some(t=>t.id===sel.value)) return sel.value;
  if(FLOW_SEL_TASK_ID&&flowAllTasks().some(t=>t.id===FLOW_SEL_TASK_ID)) return FLOW_SEL_TASK_ID;
  const runnable=flowAllTasks().find(t=>RUNNABLE_STATUSES.includes(t.status));
  const first=runnable||flowAllTasks()[0];
  FLOW_SEL_TASK_ID=first?first.id:null;
  return FLOW_SEL_TASK_ID;
}
function flowSelectedTask(){ const id=flowSelectedTaskId(); return id&&(BOARD.tasks||[]).find(t=>t.id===id); }

/* ---------- terminal "Run workflow" block — last node in the flow ---------- */
function renderFlowTerminal(el){
  const runningWhole=BOARD.run&&!BOARD.run.stage;
  const runningStage=BOARD.run&&BOARD.run.stage;
  if(runningWhole){
    const t=PROG.totals||{};
    const runningTask=(BOARD.tasks||[]).find(x=>x.id===BOARD.run.task_id);
    const tok=(t.tok_in||0)+(t.tok_out||0);
    const cost=t.cost_usd? '$'+t.cost_usd.toFixed(4) : '—';
    const stage=PROG.active? `<span class="live">▶ ${esc(PROG.active)}</span>` : 'starting…';
    el.innerHTML=`<div class="ttl"><span>▶ RUNNING WORKFLOW</span></div>
      <div class="kv"><span>task</span><b>${esc(BOARD.run.task_id)}</b></div>
      ${runningTask?`<div class="kv"><span>title</span><b style="font-weight:400;font-family:inherit">${esc(runningTask.title)}</b></div>`:''}
      <div class="kv"><span>stage</span><b>${stage}</b></div>
      <div class="kv"><span>time</span><b>${fmtDur(t.dur_ms)}</b></div>
      <div class="kv"><span>tokens</span><b>${tok}</b></div>
      <div class="kv"><span>cost</span><b>${cost}</b></div>
      <button class="ghost" onclick="stopRun()">■ Stop</button>`;
    return;
  }
  if(runningStage){
    const runningTask=(BOARD.tasks||[]).find(x=>x.id===BOARD.run.task_id);
    el.innerHTML=`<div class="ttl"><span>▶ RUN WORKFLOW</span></div>
      <div class="mut" style="font-size:11.5px">A single step is running${BOARD.run.rerun?' (rerun)':''}: `+
      `<b>${esc(BOARD.run.stage)}</b> for <b>${esc(BOARD.run.task_id)}</b>`+
      `${runningTask?' — '+esc(runningTask.title):''}.</div>
      <button class="ghost" onclick="stopRun()">■ Stop</button>`;
    return;
  }
  const all=flowAllTasks();
  if(!all.length){
    el.innerHTML=`<div class="ttl"><span>▶ RUN WORKFLOW</span></div>
      <div class="mut" style="font-size:11.5px">No tasks yet — create one, then come back here to launch it.</div>`;
    return;
  }
  const selId=flowSelectedTaskId();
  const opts=all.map(x=>`<option value="${esc(x.id)}" ${x.id===selId?'selected':''}>${esc(x.id)}: ${esc(x.title)} (${esc(x.status)})</option>`).join('');
  const task=flowSelectedTask();
  const hasBaseline=task&&task.baseline_ref;
  const isRunnable=task&&RUNNABLE_STATUSES.includes(task.status);
  el.innerHTML=`<div class="ttl"><span>▶ RUN WORKFLOW</span></div>
    <div class="mut" style="font-size:11px">Runs the active workflow (<b>${esc(S.active||'default')}</b>) end-to-end against the task you pick.</div>
    <select id="flowTaskSel" onchange="FLOW_SEL_TASK_ID=this.value;renderFlow()">${opts}</select>
    <button class="primary" onclick="runWholeWorkflow()" ${isRunnable?'':'disabled'}
      title="${isRunnable?'':'This task is '+esc(task?task.status:'')+' — not runnable. Rerun from scratch to reopen it.'}">▶ Run</button>
    ${hasBaseline?`<button class="ghost" onclick="rerunWholeWorkflow()" title="Restore to before this task's very first attempt and reopen it, then run it again">↻ Rerun from scratch</button>`:''}`;
}
async function runWholeWorkflow(){
  const taskId=flowSelectedTaskId(); if(!taskId)return;
  const activeName=S.active||'default';
  if(!confirm('Run '+taskId+' under workflow "'+activeName+'" now? An agent will '+
    'start making changes in the background.'))return;
  const task=(BOARD.tasks||[]).find(x=>x.id===taskId);
  if(task && (task.workflow||'default')!==activeName){
    await post_('/api/tasks/workflow',
      {task_id:taskId, workflow:activeName==='default'?'':activeName});
  }
  const r=await post_('/api/tasks/launch',{task_id:taskId,auto:false});
  if(!r.ok){ alert(r.error||'launch failed'); return; }
  await pollBoard(); renderFlow();
}
async function rerunWholeWorkflow(){
  const taskId=flowSelectedTaskId(); if(!taskId)return;
  if(!confirm('Rerun '+taskId+' FROM SCRATCH? This restores the working tree to '+
    'before its very first attempt (git) and clears its progress notes, then '+
    'starts the whole workflow again.'))return;
  const r=await post_('/api/tasks/rerun_workflow',{task_id:taskId});
  if(!r.ok){ alert(r.error||'rerun failed'); return; }
  await pollBoard(); renderFlow();
}

/* ---------- per-step Run/Rerun (one real agent turn, in isolation) ---------- */
// While editing a task's own plan (PLAN_MODE), Run/Rerun always target THAT
// task; otherwise they target whichever task is picked in the terminal block.
function runStepTaskId(){ return PLAN_MODE ? PLAN_MODE.taskId : flowSelectedTaskId(); }
async function runStep(stepId){
  const taskId=runStepTaskId(); if(!taskId){ alert('No runnable task to run this step for.'); return; }
  if(BOARD.run){ alert('A run is already active — stop it first.'); return; }
  if(!confirm('Run just this step for '+taskId+' now?'))return;
  const r=await post_('/api/tasks/run_step',{task_id:taskId,step_id:stepId,rerun:false});
  if(!r.ok){ alert(r.error||'failed to start'); return; }
  await pollBoard(); renderFlow();
}
async function rerunStep(stepId){
  const taskId=runStepTaskId(); if(!taskId){ alert('No runnable task to rerun this step for.'); return; }
  if(BOARD.run){ alert('A run is already active — stop it first.'); return; }
  if(!confirm('Rerun this step for '+taskId+'? This restores the working '+
    'tree to right before that step\'s last attempt (git), discarding it, then runs it again.'))return;
  const r=await post_('/api/tasks/run_step',{task_id:taskId,step_id:stepId,rerun:true});
  if(!r.ok){ alert(r.error||'failed to start'); return; }
  // A parallel-wave step whose independent rollback wasn't possible falls
  // back to reverting the WHOLE wave (see loop.rollback_parallel_step) —
  // that's never allowed to happen silently, so surface it here.
  if(r.note){ alert(r.note); }
  await pollBoard(); renderFlow();
}
/* ---------- preview/export a step's exact prompt (no side effects) ---------- */
async function viewFullContext(stepId){
  const taskId=runStepTaskId();
  if(!taskId){ alert('Select a runnable task first.'); return; }
  const r=await (await fetch(api(`/api/task_plan/step_prompt?task=${encodeURIComponent(taskId)}&step=${encodeURIComponent(stepId)}`))).json();
  if(r.error){ alert(r.error); return; }
  const w=window.open('', '_blank');
  w.document.title='Full context — '+stepId;
  w.document.body.style.cssText='white-space:pre-wrap;font-family:monospace;padding:16px;';
  w.document.body.textContent=r.prompt;
  const btn=w.document.createElement('button');
  btn.textContent='Copy to file';
  btn.style.cssText='position:fixed;top:8px;right:8px;';
  btn.onclick=async ()=>{
    const rr=await fetch(api('/api/task_plan/step_prompt/export'),{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task:taskId, step:stepId, text:r.prompt})});
    const j=await rr.json();
    if(j.error) alert(j.error); else alert('Saved to '+j.path);
  };
  w.document.body.prepend(btn);
}
function skillNames(){ return S.skills.map(s=>s.name); }
// Every tool a step could be given: tools already used on some step, PLUS
// every known built-in MCP tool and every saved custom tool — otherwise a
// custom tool a user just created has no way to be picked from a step (it
// only shows up here once some step already references it, a chicken-and-egg
// gap that made custom tools look impossible to attach to a flow step).
function allTools(){ const s=new Set();
  S.workflow.nodes.forEach(n=>(n.tools||[]).forEach(t=>s.add(t)));
  Object.keys(TOOL_DOCS||{}).forEach(t=>s.add(t));
  (CUSTOM_TOOLS||[]).forEach(t=>s.add(t.name));
  return [...s].sort(); }
function render(){ if(tab==='flow')renderFlow(); else if(tab==='skills')renderSkills(); else renderTools(); }
let Z=1;   // canvas zoom

/* numbers follow array order: each enabled or disabled STEP gets the next n */
function numbers(){ let c=0; return S.workflow.nodes.map(n=> n.kind==='step' ? (++c) : null); }
function uniqueTitle(base){ let t=base,i=2; const has=x=>S.workflow.nodes.some(n=>n.title===x);
  while(has(t)){ t=base+' '+i; i++; } return t; }

/* ---------- flow canvas ---------- */
function posFor(n,i){ return L[n.title] || {x:120, y:40+i*170}; }
function renderFlow(){
  const surf=$('#surface');
  [...surf.querySelectorAll('.node')].forEach(e=>e.remove());
  [...surf.querySelectorAll('.lane')].forEach(e=>e.remove());
  const nums=numbers();
  const busy=!!BOARD.run;
  const selTaskId=runStepTaskId();
  const selTask=selTaskId&&(BOARD.tasks||[]).find(t=>t.id===selTaskId);
  let maxBottom=0;
  S.workflow.nodes.forEach((n,i)=>{
    const p=posFor(n,i); L[n.title]=p;
    const isRunStep=n.kind==='step'&&!!n.id;
    const el=document.createElement('div');
    el.className='node'+(n===selNode?' sel':'')+(n.enabled===false?' off':'')+(isRunStep?' has-run':'');
    el.dataset.i=i;
    el.style.left=p.x+'px'; el.style.top=p.y+'px';
    const num = nums[i]!=null ? nums[i] : '•';
    const reqChips=(n.required||[]).map(s=>`<span class="chip req ${usageBadgeClass(n.id,'skills',s)}">${esc(s)}</span>`).join('');
    const recChips=(n.skills_recommended||[]).map(s=>`<span class="chip rec ${usageBadgeClass(n.id,'skills',s)}" title="recommended skill">${esc(s)}</span>`).join('');
    const toolBadge=(t,required)=>`<span class="toolbadge ${usageBadgeClass(n.id,'tools',t)}" title="${required?'required':'recommended'} tool">${esc(t)}</span>`;
    const toolNames=[...(n.tools||[]).map(t=>toolBadge(t,true)),
                      ...(n.tools_recommended||[]).map(t=>toolBadge(t,false))];
    const tools=toolNames.length?`<div class="tools">⚙ ${toolNames.join(', ')}</div>`:'';
    const onoff=n.kind==='step'
      ? `<div class="nbtn ${n.enabled!==false?'on':''}" title="enable/disable"
           onclick="toggleEnabled(${i});event.stopPropagation()">${n.enabled!==false?'●':'○'}</div>` : '';
    const hasCheckpoint=isRunStep&&selTask&&selTask.stage_checkpoints&&selTask.stage_checkpoints[n.id];
    const stepBtns=isRunStep
      ? `<div class="stepbtns" onclick="event.stopPropagation()">
          <button class="stepbtn" ${busy||!selTaskId?'disabled':''} title="Run just this step for ${selTaskId?esc(selTaskId):'the selected task'}" onclick="runStep('${esc(n.id)}');event.stopPropagation()">▶</button>
          <button class="stepbtn rerun" ${busy||!hasCheckpoint?'disabled':''} title="${hasCheckpoint?'Rerun this step — restores to right before its last attempt first':'No checkpoint yet — run this step once first'}" onclick="rerunStep('${esc(n.id)}');event.stopPropagation()">↻</button>
        </div>`
      : '';
    const allChips=reqChips+recChips;
    el.innerHTML=`${stepBtns}${onoff}<div class="ttl"><span class="num">${esc(num)}</span><span>${esc(n.title)}</span></div>
      <div class="chips">${allChips|| (n.kind==='step'?'<span class="chip">no required skills</span>':'')}</div>${tools}`;
    surf.appendChild(el);
    maxBottom=Math.max(maxBottom, p.y+140);
  });
  // Terminal "Run workflow" block — always last, connected via the same edge
  // line, never draggable (excluded from the drag/select pointerdown handler
  // by its 'terminal' class, and given an out-of-range data-i so progress/
  // edge code that indexes S.workflow.nodes[i] just skips it harmlessly).
  const term=document.createElement('div');
  term.className='node terminal'; term.dataset.i=S.workflow.nodes.length;
  term.style.left='120px'; term.style.top=(maxBottom+40)+'px';
  renderFlowTerminal(term);
  surf.appendChild(term);
  renderLanes();
  fitSurface(); redrawEdges();
  applyProgress();
  renderInsp();
  renderPlanBanner();
}
/* ---------- Phase 3: parallel-wave grouping (gesture -> field bridge) ---------- */
// On drag-end (see the pointerup handler below) the dropped node's Y position
// is compared against its neighbours; contiguous enabled steps within
// PARALLEL_Y_THRESH of each other become one "wave" sharing a `parallel`
// group id. This is a one-way bridge: Y position -> `parallel` field. The
// field (never Y) is what's saved/executed — see the design doc's "Gesture ->
// field bridge" section. Re-run on every drag-end so aligning/misaligning
// updates the field (and therefore the lane) immediately, before Save.
const PARALLEL_Y_THRESH=30;
function deriveParallelGroups(){
  const steps=S.workflow.nodes.filter(n=>n.kind==='step' && n.enabled!==false);
  const bands=[]; // [{y:lastMemberY, members:[node,...]}]
  steps.forEach(n=>{
    const y=posFor(n, S.workflow.nodes.indexOf(n)).y;
    const band=bands[bands.length-1];
    if(band && Math.abs(y-band.y)<=PARALLEL_Y_THRESH){ band.members.push(n); band.y=y; }
    else bands.push({y, members:[n]});
  });
  bands.forEach(band=>{
    if(band.members.length>=2){
      const existing=band.members.map(n=>(n.parallel||'').trim()).find(Boolean);
      const gid=existing || ('wave-'+Math.random().toString(16).slice(2,8));
      band.members.forEach(n=>n.parallel=gid);
    } else {
      band.members.forEach(n=>n.parallel='');
    }
  });
}
function parallelGroupMembers(gid){
  return S.workflow.nodes.filter(n=>n.kind==='step' && (n.parallel||'').trim()===gid);
}
function makeSequential(gid){
  parallelGroupMembers(gid).forEach(n=>n.parallel='');
  checkDirty(); renderFlow();
}
// Draws each wave's translucent backdrop BEHIND its member nodes (z-index 0
// vs .node's z-index 1, and pointer-events:none) so dragging/selecting a
// member is unaffected. Uses actual rendered DOM rects (post-layout) rather
// than raw L[] coordinates, since node height varies with content.
function renderLanes(){
  const surf=$('#surface');
  const groups={};
  S.workflow.nodes.forEach((n,i)=>{
    if(n.kind==='step' && n.enabled!==false && (n.parallel||'').trim())
      (groups[n.parallel.trim()]=groups[n.parallel.trim()]||[]).push(i);
  });
  const firstNode=surf.querySelector('.node');
  Object.entries(groups).forEach(([gid,idxs])=>{
    if(idxs.length<2) return;
    const els=idxs.map(i=>surf.querySelector(`.node[data-i="${i}"]`)).filter(Boolean);
    if(els.length<2) return;
    let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
    els.forEach(el=>{
      minX=Math.min(minX, el.offsetLeft); minY=Math.min(minY, el.offsetTop);
      maxX=Math.max(maxX, el.offsetLeft+el.offsetWidth); maxY=Math.max(maxY, el.offsetTop+el.offsetHeight);
    });
    const pad=16, headerH=22;
    const lane=document.createElement('div');
    lane.className='lane';
    lane.style.left=(minX-pad)+'px'; lane.style.top=(minY-pad-headerH)+'px';
    lane.style.width=(maxX-minX+pad*2)+'px'; lane.style.height=(maxY-minY+pad*2+headerH)+'px';
    lane.innerHTML=`<div class="lane-chip">&#8741; parallel &middot; ${esc(gid)}</div>`;
    surf.insertBefore(lane, firstNode||surf.firstChild);
  });
}
function fitSurface(){
  let mx=1200,my=900;
  document.querySelectorAll('.node').forEach(e=>{
    mx=Math.max(mx,e.offsetLeft+e.offsetWidth+200);
    my=Math.max(my,e.offsetTop+e.offsetHeight+200);
  });
  const s=$('#surface'); s.style.width=mx+'px'; s.style.height=my+'px';
}
// Collapses consecutive same-group step nodes into one "wave unit" so edges
// fan OUT from the step before a wave to every member, and re-converge from
// every member into the step after — a plain linear chain is just every unit
// having exactly one member, so this subsumes the old behavior unchanged.
function redrawEdges(){
  const nodes=[...document.querySelectorAll('.node')].sort((a,b)=>a.dataset.i-b.dataset.i);
  const units=[];
  nodes.forEach(el=>{
    const n=S.workflow.nodes[+el.dataset.i];
    const gid=(n && n.kind==='step' && n.enabled!==false && (n.parallel||'').trim()) || '';
    const last=units[units.length-1];
    if(gid && last && last.gid===gid) last.els.push(el);
    else units.push({gid, els:[el]});
  });
  let d='';
  const edge=(a,b)=>{
    const x1=a.offsetLeft+a.offsetWidth/2, y1=a.offsetTop+a.offsetHeight;
    const x2=b.offsetLeft+b.offsetWidth/2, y2=b.offsetTop;
    const dy=Math.max(30,Math.abs(y2-y1)/2);
    return `<path class="edge" d="M${x1} ${y1} C ${x1} ${y1+dy} ${x2} ${y2-dy} ${x2} ${y2}"/>`
      +`<circle cx="${x2}" cy="${y2}" r="3" fill="#3a4150"/>`;
  };
  for(let u=0;u<units.length-1;u++){
    units[u].els.forEach(a=>units[u+1].els.forEach(b=>{ d+=edge(a,b); }));
  }
  $('#edges').innerHTML=d;
}
/* order = top-to-bottom, then left-to-right on the canvas → drag to reorder */
function resortByPosition(){
  S.workflow.nodes.sort((a,b)=>{
    const pa=L[a.title]||{x:1e9,y:1e9}, pb=L[b.title]||{x:1e9,y:1e9};
    return (pa.y-pb.y)||(pa.x-pb.x);
  });
}
function autoArrange(){
  resortByPosition();   // may reorder S.workflow.nodes -> checkDirty catches that
  S.workflow.nodes.forEach((n,i)=>{ L[n.title]={x:120,y:40+i*170}; });
  renderFlow(); saveLayout(); checkDirty();
}
function addStep(){
  let my=40; document.querySelectorAll('.node').forEach(e=>my=Math.max(my,e.offsetTop+e.offsetHeight));
  const n={title:uniqueTitle('New step'),body:'',required:[],tools:[],kind:'step',enabled:true,
    id:'',agent:'',model:'',effort:'',temperature:'',type:'',command:'',on_fail:'',parallel:''};
  L[n.title]={x:120,y:my+50};
  S.workflow.nodes.push(n); selNode=n; bodyMode='write'; checkDirty(); renderFlow(); saveLayout();
}
function toggleEnabled(i){ const n=S.workflow.nodes[i]; n.enabled=n.enabled===false; checkDirty(); renderFlow(); }
function deleteNode(){
  if(!selNode) return;
  const i=S.workflow.nodes.indexOf(selNode); if(i<0) return;
  delete L[selNode.title]; S.workflow.nodes.splice(i,1); selNode=null;
  checkDirty(); renderFlow(); saveLayout();
}

/* ---------- drag nodes + pan canvas ---------- */
let drag=null, pan=null;
$('#surface').addEventListener('pointerdown',e=>{
  if(e.target.closest('.nbtn,button,input,textarea,a,.tog,select')) return;
  const node=e.target.closest('.node');
  if(node&&node.classList.contains('terminal')) return;   // not a workflow step — never draggable/selectable
  if(node){
    const i=+node.dataset.i; selNode=S.workflow.nodes[i]; bodyMode='preview'; highlight(); renderInsp();
    drag={el:node,title:selNode.title,sx:e.clientX,sy:e.clientY,
          ox:node.offsetLeft,oy:node.offsetTop,moved:false};
    node.classList.add('drag'); node.setPointerCapture(e.pointerId);
    e.preventDefault();
  }
});
$('#canvas').addEventListener('pointerdown',e=>{
  if(e.target.closest('.node')) return;
  const c=$('#canvas');
  pan={sx:e.clientX,sy:e.clientY,sl:c.scrollLeft,st:c.scrollTop};
  c.style.cursor='grabbing';
});
window.addEventListener('pointermove',e=>{
  if(drag){
    const nx=Math.max(0,drag.ox+(e.clientX-drag.sx)/Z);
    const ny=Math.max(0,drag.oy+(e.clientY-drag.sy)/Z);
    drag.el.style.left=nx+'px'; drag.el.style.top=ny+'px';
    L[drag.title]={x:nx,y:ny}; drag.moved=true; redrawEdges();
  }else if(pan){
    const c=$('#canvas');
    c.scrollLeft=pan.sl-(e.clientX-pan.sx);
    c.scrollTop =pan.st-(e.clientY-pan.sy);
  }
});
window.addEventListener('pointerup',()=>{
  if(drag){
    drag.el.classList.remove('drag');
    if(drag.moved){ resortByPosition(); deriveParallelGroups(); renderFlow(); saveLayout(); checkDirty(); } // reorder → renumber → re-band waves
    drag=null;
  }
  if(pan){ $('#canvas').style.cursor=''; pan=null; }
});
function highlight(){ document.querySelectorAll('.node').forEach(e=>
  e.classList.toggle('sel', S.workflow.nodes[+e.dataset.i]===selNode)); }
async function saveLayout(){
  await fetch(api('/api/layout'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(L)});
}

/* ---------- zoom ---------- */
function applyZoom(){ $('#surface').style.transform='scale('+Z+')';
  $('#zlbl').textContent=Math.round(Z*100)+'%'; }
function setZoom(z, cx, cy){
  z=Math.max(0.4,Math.min(2,z)); if(z===Z) return;
  const c=$('#canvas'), r=c.getBoundingClientRect();
  cx=cx==null? r.width/2 : cx-r.left; cy=cy==null? r.height/2 : cy-r.top;
  const px=(c.scrollLeft+cx)/Z, py=(c.scrollTop+cy)/Z;   // surface point under cursor
  Z=z; applyZoom();
  c.scrollLeft=px*Z-cx; c.scrollTop=py*Z-cy;             // keep that point fixed
}
function zoomBy(f){ setZoom(Z*f); }
function zoomReset(){ setZoom(1); }
$('#canvas').addEventListener('wheel',e=>{
  if(tab!=='flow') return;
  // Plain wheel / two-finger scroll → let the canvas scroll natively (no zoom).
  // Trackpad PINCH arrives as a wheel event with ctrlKey set → that zooms.
  if(!e.ctrlKey) return;
  e.preventDefault();
  setZoom(Z*Math.exp(-e.deltaY*0.01), e.clientX, e.clientY);
},{passive:false});

/* ---------- markdown rendering (Notion-like, dependency-free) ---------- */
function mdToHtml(src){
  if(!src||!src.trim()) return '';
  const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const blocks=[];
  src=src.replace(/```([\s\S]*?)```/g,(m,c)=>{ blocks.push('<pre><code>'+esc(c.replace(/^\n/,''))+'</code></pre>'); return ''+(blocks.length-1)+''; });
  const inline=t=>{ t=esc(t);
    t=t.replace(/`([^`]+)`/g,(m,c)=>'<code>'+c+'</code>');
    t=t.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
    t=t.replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>');
    t=t.replace(/\[([^\]]+)\]\(([^)]+)\)/g,(m,x,u)=>{ u=u.trim(); if(/^javascript:/i.test(u))u='#';
      return '<a href="'+u+'" target="_blank" rel="noopener">'+x+'</a>'; });
    return t; };
  const lines=src.split('\n'), out=[]; let i=0, para=[];
  const flush=()=>{ if(para.length){ out.push('<p>'+para.map(inline).join('<br>')+'</p>'); para=[]; } };
  while(i<lines.length){ const ln=lines[i];
    const ph=ln.match(/^(\d+)$/);
    if(ph){ flush(); out.push(blocks[+ph[1]]); i++; continue; }
    if(/^\s*$/.test(ln)){ flush(); i++; continue; }
    const h=ln.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flush(); const l=h[1].length; out.push('<h'+l+'>'+inline(h[2])+'</h'+l+'>'); i++; continue; }
    if(/^\s*(---|\*\*\*|___)\s*$/.test(ln)){ flush(); out.push('<hr>'); i++; continue; }
    if(/^\s*>\s?/.test(ln)){ flush(); const q=[]; while(i<lines.length&&/^\s*>\s?/.test(lines[i])){ q.push(lines[i].replace(/^\s*>\s?/,'')); i++; } out.push('<blockquote>'+q.map(inline).join('<br>')+'</blockquote>'); continue; }
    if(/^\s*[-*+]\s+/.test(ln)){ flush(); const it=[]; while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i])){ it.push('<li>'+inline(lines[i].replace(/^\s*[-*+]\s+/,''))+'</li>'); i++; } out.push('<ul>'+it.join('')+'</ul>'); continue; }
    if(/^\s*\d+\.\s+/.test(ln)){ flush(); const it=[]; while(i<lines.length&&/^\s*\d+\.\s+/.test(lines[i])){ it.push('<li>'+inline(lines[i].replace(/^\s*\d+\.\s+/,''))+'</li>'); i++; } out.push('<ol>'+it.join('')+'</ol>'); continue; }
    para.push(ln); i++;
  }
  flush();
  return out.join('\n');
}
/* one body editor at a time (the selected node OR skill) — click to edit, blur to render */
function getBody(){ return tab==='flow' ? (selNode?selNode.body||'':'') : (S.skills[skillSel]?S.skills[skillSel].body||'':''); }
function setBody(v){ if(tab==='flow'){ if(selNode){selNode.body=v; checkDirty();} } else if(S.skills[skillSel]){ S.skills[skillSel].body=v; } }
function reInsp(){ tab==='flow'?renderInsp():renderSkillEditor(); }
function bodyHtml(minH){ minH=minH||140;
  if(bodyMode==='write')
    return `<textarea id="bodyTA" style="min-height:${minH}px" oninput="setBody(this.value)" onblur="doneBody()">${esc(getBody())}</textarea>`;
  const html=mdToHtml(getBody());
  return `<div class="mdview ${html?'':'empty'}" style="min-height:${minH}px" onclick="editBody()" title="click to edit">`
    + (html?`<div class="md">${html}</div>`:'click to write…') + `</div>`;
}
function editBody(){ bodyMode='write'; reInsp();
  const t=$('#bodyTA'); if(t){ t.focus(); t.setSelectionRange(t.value.length,t.value.length); } }
function doneBody(){ bodyMode='preview'; reInsp(); }

/* ---------- inspector ---------- */
function renderInsp(){
  if(tab==='skills'){ renderSkillEditor(); return; }
  const n=selNode; if(!n){ $('#insp').innerHTML='<div class="empty">Select a node to edit it.</div>'; return; }
  const isStep=n.kind==='step';
  const toggles=skillNames().map(name=>{
    const on=(n.required||[]).includes(name);
    return `<span class="tog ${on?'on':''} ${on?usageBadgeClass(n.id,'skills',name):''}" onclick="toggleReq('${esc(name)}')">${esc(name)}</span>`;
  }).join('');
  const reqLinks=(n.required||[]).map(name=>`<a class="link" onclick="editSkill('${esc(name)}')">edit ${esc(name)} »</a>`).join(' · ');
  const toolTogs=allTools().map(t=>{
    const on=(n.tools||[]).includes(t);
    return `<span class="tog ${on?'on':''} ${on?usageBadgeClass(n.id,'tools',t):''}" title="${esc(toolDoc(t))}" onclick="toggleTool('${esc(t)}')">${esc(t)}</span>`;
  }).join('');
  // Recommended skills/tools (Task 1's `recommended:` tier) have no editing UI
  // yet — surfaced here read-only, purely so their usage badge (Task 7) is
  // visible somewhere; toggling them on/off is a separate, not-yet-built
  // feature and out of this task's additive scope.
  const recSkillChips=(n.skills_recommended||[]).map(name=>
    `<span class="chip rec ${usageBadgeClass(n.id,'skills',name)}" title="recommended skill">${esc(name)}</span>`).join('');
  const recToolChips=(n.tools_recommended||[]).map(t=>
    `<span class="chip rec ${usageBadgeClass(n.id,'tools',t)}" title="recommended tool">${esc(t)}</span>`).join('');
  // Shown on EVERY step: pick the AGENT + MODEL this step runs with, and
  // Run/Rerun it in isolation — no more "which pipeline stage is this"
  // gating; any step can be run on its own via loop.run_step (by id).
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
      oninput="setStepField('command',this.value,false)" placeholder="npm test">${esc(n.command||'')}</textarea>
    <label>On fail <span class="mut">(dispatch this agent step, then retry the command)</span></label>
    <select onchange="setStepField('on_fail',this.value)">${onFailOpts}</select>
    ${runBtns}
    ${lastRunOutputBlock(n)}
    <div class="mut" style="font-size:11px;margin-top:6px;line-height:1.6">
      Failure = non-zero exit or timeout. With no On-fail target, a failed
      command step just records its output for the next step to see — same as
      today's default behavior.
    </div>`;
  })():'';
  const modelSection=isStep?(()=>{
    const runNote=`
      <div class="mut" style="font-size:11px;margin-top:6px;line-height:1.6">
        Applies to <b>harn run</b> (headless). Default agent/model set in
        <a class="link" onclick="showTab('settings')">Settings</a>; override here
        per step. In chat mode (Cursor/Claude Code) the agent is your own IDE —
        harn can't switch it.
      </div>`;
    const ch=stepChoices(n);
    const agentOpts=`<option value="" ${!n.agent?'selected':''}>Default: ${esc(AGENT_LABEL[MODELS.default_agent]||MODELS.default_agent||'(unset)')}</option>`
      + allAgentNames().map(a=>`<option value="${esc(a)}" ${n.agent===a?'selected':''}>${esc(AGENT_LABEL[a]||a)}${agentCaps(a).available?'':' (not installed)'}</option>`).join('');
    const taskId=runStepTaskId();
    const busy=!!BOARD.run;
    const runBtns=n.id?`
    <div class="row" style="margin-top:8px;gap:10px">
      <button ${busy||!taskId?'disabled':''} title="${taskId?'Run just this step for '+esc(taskId):'No task selected'}" onclick="runStep('${esc(n.id)}')">▶ Run step</button>
      <button ${busy||!taskId?'disabled':''} title="${taskId?'Rerun this step for '+esc(taskId):'No task selected'}" onclick="rerunStep('${esc(n.id)}')">↻ Rerun step</button>
    </div>` : '';
    return `
    <label>Agent &amp; model <span class="mut">for this step</span></label>
    <div class="modelrow4">
      <select onchange="setStepField('agent',this.value)" title="which CLI runs this step">${agentOpts}</select>
      <div>${selectOrCustom(n.id||'new','model',n.model||'',ch.models)}</div>
      <div>${selectOrCustom(n.id||'new','effort',n.effort||'',ch.efforts)}</div>
      <div>${selectOrCustom(n.id||'new','temperature',n.temperature||'',ch.temperatures)}</div>
    </div>${runBtns}${lastRunOutputBlock(n)}${runNote}`;
  })():'';
  // Step-type-agnostic (applies whether Type is agent or command): a note +
  // escape hatch for steps dragged into a parallel wave (see deriveParallelGroups
  // / renderLanes above). Group membership is driven purely by the `parallel`
  // field, so this reads the same source of truth the canvas lane draws from.
  const parallelNote=(isStep && (n.parallel||'').trim())?(()=>{
    const gid=n.parallel.trim();
    const others=parallelGroupMembers(gid).length-1;
    return `
    <div class="mut" style="font-size:11.5px;margin-top:14px;padding:10px 12px;
      border:1px solid var(--accent);border-radius:8px;background:rgba(124,140,255,.08);line-height:1.6">
      &#8741; Part of parallel group <b style="color:var(--text)">${esc(gid)}</b> — runs concurrently
      with ${others} other step${others===1?'':'s'}.
      <div style="margin-top:8px"><button onclick="makeSequential('${esc(gid)}')">Make sequential</button></div>
    </div>`;
  })():'';
  // Preview/export the EXACT prompt this step would (or did) receive — no
  // side effects, calls loop.preview_step_prompt via the studio API.
  const viewContextBtn=(isStep && n.id)?`
    <div class="row" style="margin-top:8px">
      <button class="ghost" onclick="viewFullContext('${esc(n.id)}')">View full context</button>
    </div>` : '';
  $('#insp').innerHTML=`
    <h2>${isStep?'Step':'Note'}</h2>
    <div class="row" style="justify-content:space-between">
      <label style="margin:0">${isStep?'<input type="checkbox" '+(n.enabled!==false?'checked':'')+' onchange="setEnabled(this.checked)"/> enabled':''}</label>
      <button class="icon-btn" onclick="deleteNode()" title="Delete this step">${TRASH_SVG}</button>
    </div>
    <label>Title</label>
    <input type="text" value="${esc(n.title)}" oninput="upd('title',this.value)"/>
    <label>Description / steps</label>
    ${bodyHtml(140)}
    ${isStep?`
    <label>Skills required at this step <span class="mut">(click to toggle)</span></label>
    <div class="skillgrid">${toggles||'<span class="mut">no skills yet</span>'}</div>
    <div style="margin-top:8px">${reqLinks}</div>
    ${recSkillChips?`<label>Recommended skills <span class="mut">(surfaced, not force-loaded)</span></label>
    <div class="skillgrid">${recSkillChips}</div>`:''}
    <label>Tools at this step <span class="mut">(click to toggle · add below)</span></label>
    <div class="skillgrid">${toolTogs||'<span class="mut">no tools yet</span>'}</div>
    <input type="text" placeholder="add a tool, press Enter" style="margin-top:8px"
      onkeydown="if(event.key==='Enter'){addTool(this.value);this.value='';}"/>
    ${recToolChips?`<label>Recommended tools</label>
    <div class="skillgrid">${recToolChips}</div>`:''}
    ${parallelNote}
    ${viewContextBtn}
    ${typeToggle}${stepType==='agent'?modelSection:commandSection}`:''}
  `;
}
function upd(k,v){
  if(!selNode) return; const old=selNode.title;
  selNode[k]=v; checkDirty();
  if(k==='title'){
    if(L[old]){ L[v]=L[old]; if(v!==old) delete L[old]; }
    // Update the selected node's on-canvas label directly instead of a full
    // renderFlow() — the latter also re-runs renderInsp(), which rebuilds the
    // title <input> being typed into and drops focus after each character.
    const lbl=document.querySelector('#surface .node.sel .ttl span:last-child');
    if(lbl) lbl.textContent=v;
  }
}
function setEnabled(on){ if(!selNode)return; selNode.enabled=on; checkDirty(); renderFlow(); }
function toggleReq(name){
  if(!selNode)return; selNode.required=selNode.required||[];
  const k=selNode.required.indexOf(name); if(k>=0)selNode.required.splice(k,1); else selNode.required.push(name);
  checkDirty(); renderFlow();
}
function toggleTool(name){
  if(!selNode)return; selNode.tools=selNode.tools||[];
  const k=selNode.tools.indexOf(name); if(k>=0)selNode.tools.splice(k,1); else selNode.tools.push(name);
  checkDirty(); renderFlow();
}
function addTool(v){ v=(v||'').trim(); if(!v||!selNode)return;
  selNode.tools=selNode.tools||[]; if(!selNode.tools.includes(v))selNode.tools.push(v);
  checkDirty(); renderFlow(); }
async function saveFlow(){
  setStatus('saving…'); resortByPosition();
  // Editing a task's own plan (PLAN_MODE) saves to that task's snapshot file
  // ONLY — never the shared preset. Otherwise this is the normal preset save.
  const r = PLAN_MODE
    ? await fetch(api('/api/task_plan'),{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({task_id:PLAN_MODE.taskId, plan:S.workflow})})
    : await fetch(api('/api/workflow'),{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(S.workflow)});
  const j=await r.json();
  if(j.ok){
    // The backend stamps step ids on first save (ensure_ids) — sync them back
    // into S.workflow so Run/Rerun + the model inspector work immediately,
    // with no page reload needed.
    if(j.workflow) S.workflow={preamble:j.workflow.preamble||'', nodes:j.workflow.nodes||[]};
    else if(j.plan) S.workflow=j.plan;
    snapshotWorkflow(); clearDirty(); setStatus('saved ✓');
  } else { setStatus('save failed'); }
  renderFlow();
}

/* ---------- task-plan edit mode: Board -> "Edit this task's plan" ---------- */
// Loads ONE task's own workflow snapshot into the SAME Flow canvas used for
// preset editing. saveFlow() detects PLAN_MODE and posts to /api/task_plan
// instead of /api/workflow, so preset files are never touched from here.
async function openTaskPlan(taskId){
  if(dirty && !confirm('Unsaved '+(PLAN_MODE?'plan':'workflow')+' edits will be lost. Continue?')) return;
  const r=await (await fetch(api('/api/task_plan?task='+encodeURIComponent(taskId)))).json();
  if(!r.ok){ alert(r.error||'could not load this task\'s plan'); return; }
  PLAN_MODE={taskId};
  S.workflow=r.plan;
  L=Object.assign({}, S.layout||{});
  selNode=null;
  showTab('flow');
  snapshotWorkflow(); clearDirty();
  renderPlanBanner();
}
function closeTaskPlan(){
  PLAN_MODE=null;
  $('#planBanner').style.display='none';
}
function renderPlanBanner(){
  const b=$('#planBanner');
  if(!PLAN_MODE){ b.style.display='none'; return; }
  b.style.display='flex';
  b.innerHTML=`<span>✎ editing plan of <b>${esc(PLAN_MODE.taskId)}</b> — changes affect only this task</span>
    <button class="ghost" onclick="showTab('board')">Done</button>`;
}

/* ---------- tabs + list views ---------- */
function showTab(t){
  if(t!=='flow' && PLAN_MODE){
    if(dirty && !confirm('Unsaved plan edits will be lost. Leave anyway?')){ return; }
    closeTaskPlan();
  }
  tab=t; bodyMode='preview';
  ['flow','skills','tools','board','settings'].forEach(x=>$('#tab'+x[0].toUpperCase()+x.slice(1)).classList.toggle('active',x===t));
  $('#surface').style.display = t==='flow'?'':'none';
  $('#listView').style.display = t==='flow'?'none':'block';
  $('#zoom').style.display = t==='flow'?'':'none';
  $('#canvas').classList.toggle('list',t!=='flow');
  $('#saveBtn').style.display=t==='flow'?'':'none';
  $('#arrangeBtn').style.display=t==='flow'?'':'none';
  $('#addBtn').style.display=t==='flow'?'':'none';
  $('#addSkillBtn').style.display=t==='skills'?'':'none';
  if(t==='flow')renderFlow();
  else if(t==='skills')renderSkills();
  else if(t==='tools')renderTools();
  else if(t==='settings')renderSettings();
  else{ pollBoard(); }
}

/* ---------- settings tab: default agent + model for harn run ---------- */
async function renderSettings(){
  await ensureModelsLoaded();
  const v=$('#listView');
  const da=MODELS.default_agent||'', dm=MODELS.default_model||'';
  const agentOpts=allAgentNames().map(a=>{
    const c=agentCaps(a);
    return `<option value="${esc(a)}" ${da===a?'selected':''}>${esc(AGENT_LABEL[a]||a)}${c.available?' ✓ installed':' — not installed'}</option>`;
  }).join('');
  const dmModels=(agentCaps(da).models)||[];
  v.innerHTML=`<div class="settings">
    <h2>SETTINGS — harn run</h2>
    <p class="mut" style="font-size:12px;line-height:1.6">
      Which agent CLI drives <b>harn run</b> (headless), and the default model.
      This is what makes harn run connect Cursor / Codex / Claude / … as you
      choose. Each pipeline stage can override this in its step on the Flow tab.
    </p>
    <div class="field">
      <label>Default agent <span class="mut">(the CLI harn run launches)</span></label>
      <select id="setAgent" onchange="onDefaultAgentChange(this.value)">${agentOpts||'<option>(none)</option>'}</select>
    </div>
    <div class="field">
      <label>Default model <span class="mut">(applied to every stage without its own override; blank = the CLI's own default)</span></label>
      <div id="setModelWrap">${settingsModelSelect(dm,dmModels)}</div>
    </div>
    <div class="row" style="margin-top:14px;gap:10px">
      <button class="primary" onclick="saveDefaults()">Save settings</button>
      <span class="status" id="setStatus"></span>
    </div>
    <p class="mut" style="font-size:11px;margin-top:16px;line-height:1.6">
      Note: in <b>chat mode</b> (you working inside Cursor or Claude Code) the
      agent and model are whatever your IDE session uses — harn can't switch
      them. These defaults govern <b>harn run</b> only.
    </p>
  </div>`;
  $('#insp').innerHTML='<div class="empty">harn run defaults. Per-stage overrides live on each Flow step.</div>';
}
function settingsModelSelect(current,options){
  const isCustom=!!current && !options.includes(current);
  return `<select id="setModel" onchange="onDefaultModelSelect(this)">
      <option value="">(CLI default)</option>
      ${options.map(o=>`<option value="${esc(o)}" ${current===o?'selected':''}>${esc(o)}</option>`).join('')}
      <option value="__custom__" ${isCustom?'selected':''}>Custom…</option>
    </select>
    <input type="text" id="setModelCustom" placeholder="custom model" value="${esc(isCustom?current:'')}"
      style="margin-top:5px;${isCustom?'':'display:none'}"/>`;
}
function onDefaultAgentChange(agentName){
  MODELS.default_agent=agentName;
  // model options depend on the agent — re-render just the model field
  const wrap=$('#setModelWrap');
  if(wrap) wrap.innerHTML=settingsModelSelect('', (agentCaps(agentName).models)||[]);
}
function onDefaultModelSelect(sel){
  const inp=$('#setModelCustom');
  if(sel.value==='__custom__'){ if(inp){inp.style.display='';inp.value='';inp.focus();} }
  else if(inp){ inp.style.display='none'; }
}
async function saveDefaults(){
  const agent=$('#setAgent')?$('#setAgent').value:'';
  const msel=$('#setModel')?$('#setModel').value:'';
  const model = msel==='__custom__' ? ($('#setModelCustom')?$('#setModelCustom').value.trim():'') : msel;
  $('#setStatus').textContent='saving…';
  const r=await post_('/api/defaults',{agent,model});
  if(r.ok){ MODELS.default_agent=r.agent||agent; MODELS.default_model=r.model||model;
    $('#setStatus').textContent='saved ✓'; }
  else $('#setStatus').textContent=r.error||'save failed';
}

/* ---------- skills tab ---------- */
function renderSkills(){
  const v=$('#listView'); v.innerHTML='<h2>SKILLS</h2>';
  S.skills.forEach((s,i)=>{ const r=document.createElement('div');r.className='skillrow';
    r.onclick=()=>{skillSel=i;bodyMode='preview';renderSkillEditor();};
    r.innerHTML=`<div><div class="nm">${esc(s.name)}</div><div class="ds">${esc(s.description||'')}</div></div>`;
    v.appendChild(r); });
  if(!S.skills.length) v.innerHTML+='<div class="empty">No skills yet — “＋ Add skill”.</div>';
  if(skillSel>=0&&skillSel<S.skills.length) renderSkillEditor();
  else $('#insp').innerHTML='<div class="empty">Select a skill to edit it.</div>';
}
function editSkill(name){ showTab('skills'); skillSel=S.skills.findIndex(s=>s.name===name);
  renderSkills(); renderSkillEditor(); }
function goToNode(title){
  showTab('flow');
  selNode=S.workflow.nodes.find(n=>n.title===title)||selNode;
  highlight(); renderInsp();
  const el=document.querySelector('.node.sel');
  if(el){ const c=$('#canvas');
    c.scrollLeft=el.offsetLeft*Z - c.clientWidth/2 + el.offsetWidth*Z/2;
    c.scrollTop =el.offsetTop*Z  - c.clientHeight/2 + el.offsetHeight*Z/2; }
}
function addSkill(){
  let base='new-skill',n=base,i=2; while(S.skills.some(s=>s.name===n)){ n=base+'-'+i;i++; }
  S.skills.push({name:n,description:'',body:'# '+n+'\n\n'}); skillSel=S.skills.length-1;
  bodyMode='write'; renderSkills(); renderSkillEditor();
}
function renderSkillEditor(){
  const s=S.skills[skillSel]; if(!s){ $('#insp').innerHTML='<div class="empty">Select a skill.</div>'; return; }
  $('#insp').innerHTML=`
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Skill</h2>
      <button class="icon-btn" onclick="deleteSkill(${skillSel})" title="Delete this skill">${TRASH_SVG}</button>
    </div>
    <label>Name <span class="mut">(slug)</span></label>
    <input type="text" value="${esc(s.name)}" oninput="S.skills[${skillSel}].name=this.value.trim().toLowerCase().replace(/\s+/g,'-')"/>
    <label>Description</label>
    <input type="text" value="${esc(s.description||'')}" oninput="S.skills[${skillSel}].description=this.value"/>
    <label>Body (Markdown)</label>
    ${bodyHtml(300)}
    <div class="row" style="margin-top:12px">
      <button class="primary" onclick="saveSkill(${skillSel})">Save skill</button>
      <button class="ghost" onclick="location.href=api('/api/skill/export?name='+encodeURIComponent(s.name))">Export</button>
      <span class="status" id="sst"></span>
    </div>
    ${(()=>{ const users=S.workflow.nodes.filter(n=>(n.required||[]).includes(s.name));
      return `<label>Used by ${users.length} step(s)</label>
    <div class="skillgrid">${users.map(u=>`<span class="chip" style="cursor:pointer" onclick="goToNode('${esc(u.title)}')">${esc(u.title)}</span>`).join('')||'<span class="mut">none</span>'}</div>`; })()}`;
}
async function saveSkill(i){
  const s=S.skills[i]; if(!s.name){ $('#sst').textContent='name required'; return; }
  $('#sst').textContent='saving…';
  const r=await fetch(api('/api/skill'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:s.name,description:s.description,body:s.body})});
  const j=await r.json(); renderSkills();
  const st=$('#sst'); if(st) st.textContent=j.ok?'saved ✓':'failed';
}
async function deleteSkill(i){
  const s=S.skills[i]; if(!confirm('Delete skill "'+s.name+'"?'))return;
  await fetch(api('/api/skill/delete'),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:s.name})});
  // also strip it from every step's required
  S.workflow.nodes.forEach(n=>{ if(n.required){const k=n.required.indexOf(s.name);if(k>=0)n.required.splice(k,1);} });
  checkDirty();
  S.skills.splice(i,1); skillSel=Math.min(skillSel,S.skills.length-1);
  $('#insp').innerHTML='<div class="empty">Skill deleted.</div>'; renderSkills();
}

/* ---------- tools tab (tools live in steps; edited via the workflow) ---------- */
let toolSel=null, customToolSel=null;
function selectTool(t){ toolSel=t; customToolSel=null; renderTools(); }
function renderTools(){
  const v=$('#listView'); const tools=allTools();
  // Build the WHOLE #listView as one string assignment. Mixing
  // `document.createElement`+`.onclick`-property rows with a later
  // `v.innerHTML+=...` serializes the DOM back to a string and reparses it —
  // that reparse throws away any handler attached as a JS property (only
  // `onclick="..."` HTML-attribute handlers survive), silently making every
  // row unclickable the moment a second section (custom tools / chat panel)
  // gets appended below it. Using `onclick="selectTool(...)"` string
  // attributes throughout — the same convention every other list in this
  // file already uses — avoids the trap entirely.
  const rows=tools.map(t=>{
    const users=S.workflow.nodes.filter(n=>(n.tools||[]).includes(t));
    const doc=toolDoc(t); const first=doc.split('\n')[0];
    return `<div class="skillrow${toolSel===t?' sel':''}" title="${esc(doc)}" onclick="selectTool('${esc(t)}')">`+
      `<div style="width:100%"><div class="nm">${esc(t)} <span class="mut" style="font-weight:400">· ${users.length} step(s)</span></div>`+
      `<div class="ds">${esc(first)}</div></div></div>`;
  }).join('');
  v.innerHTML='<h2>TOOLS <span class="mut" style="text-transform:none;letter-spacing:0">— every tool available to add to a step; Save flow to persist step changes</span></h2>'+
    (rows || '<div class="empty">No tools yet — add tools on a step (Flow tab).</div>')+
    renderCustomToolsSection()+
    renderToolChatPanel();
  if(customToolSel && CUSTOM_TOOLS.some(t=>t.name===customToolSel)) renderCustomToolEditor();
  else if(toolSel&&tools.includes(toolSel)) renderToolEditor();
  else $('#insp').innerHTML='<div class="empty">Select a tool.</div>';
}
/* ---------- custom tools: upload / delete (Phase 5) ---------- */
function renderCustomToolsSection(){
  const rows=CUSTOM_TOOLS.map(t=>
    `<div class="skillrow${customToolSel===t.name?' sel':''}" onclick="customToolSel='${esc(t.name)}';toolSel=null;renderCustomToolEditor();renderTools();"><div style="width:100%">`+
    `<div class="nm">${esc(t.name)} <span class="mut" style="font-weight:400">(${esc(t.source)})</span></div>`+
    `<div class="ds">${esc(t.description)}</div>`+
    `</div></div>`).join('');
  return `<h2 style="margin-top:18px">CUSTOM TOOLS</h2>`+
    (rows||'<div class="empty">None yet.</div>')+
    `<button class="ghost" style="margin-top:8px" onclick="$('#toolImportInput').click()">＋ Import</button>`+
    `<input type="file" id="toolImportInput" style="display:none" onchange="importOrUploadToolFile(this)"/>`;
}
function renderCustomToolEditor(){
  const t=CUSTOM_TOOLS.find(x=>x.name===customToolSel);
  if(!t){ $('#insp').innerHTML='<div class="empty">Select a tool.</div>'; return; }
  $('#insp').innerHTML=`
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Custom tool</h2>
      <button class="icon-btn" onclick="deleteCustomTool('${esc(t.name)}')" title="Delete this tool">${TRASH_SVG}</button>
    </div>
    <label>Name</label>
    <div class="toolDoc">${esc(t.name)}</div>
    <label>Source</label>
    <div class="toolDoc">${esc(t.source)}</div>
    <label>Description</label>
    <div class="toolDoc">${esc(t.description||'(none)')}</div>
    <label>Params</label>
    <div class="toolDoc">${esc((t.params||[]).join(', ')||'(none)')}</div>
    <label>Command</label>
    <div class="toolDoc" style="font-family:ui-monospace,Menlo,monospace">${esc(t.command||'(none)')}</div>
    <div class="row" style="margin-top:12px;gap:8px">
      <button class="primary" onclick="location.href=api('/api/tools/export?name='+encodeURIComponent('${esc(t.name)}'))">Export</button>
      <button class="ghost" onclick="deleteCustomTool('${esc(t.name)}')">Delete</button>
    </div>
    <div class="mut" style="margin-top:14px">Available to the agent starting its next session.</div>`;
}
async function importOrUploadToolFile(input){
  const file=input.files&&input.files[0]; if(!file)return;
  const dataUrl=await new Promise((res,rej)=>{
    const r=new FileReader(); r.onload=()=>res(r.result); r.onerror=rej; r.readAsDataURL(file);
  });
  const content_b64=dataUrl.split(',')[1]||'';
  let isBundle=/\.zip$/i.test(file.name);
  if(!isBundle && /\.json$/i.test(file.name)){
    try{
      const text=atob(content_b64);
      const parsed=JSON.parse(text);
      isBundle = parsed && typeof parsed==='object' && 'name' in parsed && 'command' in parsed;
    }catch(e){ isBundle=false; }
  }
  if(isBundle){
    const r=await post_('/api/tools/import',{filename:file.name,content_b64});
    if(!r.ok){ alert(r.error||'import failed'); return; }
    input.value='';
    await loadToolsData(true);
    renderTools();
    alert('Imported "'+r.name+'". Available to the agent starting its next session.');
    return;
  }
  const name=prompt('Tool name (a-z0-9_ only):'); if(!name){ input.value=''; return; }
  const description=prompt('Description:')||'';
  const paramsRaw=prompt('Comma-separated param names (or leave blank):')||'';
  const params=paramsRaw.split(',').map(s=>s.trim()).filter(Boolean);
  const argList=params.map(p=>'{'+p+'}').join(' ');
  const r=await post_('/api/tools/save',{name,description,params,
    command:`bash ${file.name} ${argList}`.trim(), source:'upload', script_name:file.name, content_b64});
  if(!r.ok){ alert(r.error||'save failed'); return; }
  input.value='';
  await loadToolsData(true);
  renderTools();
  alert('Saved. This tool will be available to the agent starting its NEXT session — not the one currently running.');
}
async function deleteCustomTool(name){
  if(!confirm('Delete "'+name+'"?'))return;
  await post_('/api/tools/delete',{name});
  if(customToolSel===name) customToolSel=null;
  await loadToolsData(true);
  renderTools();
}
/* ---------- custom tools: agent-chat drafting (Phase 5 Task 6) ---------- */
let TOOL_CHAT_HISTORY=[];   // [{role,text}], reset on panel open/tool save
let TOOL_CHAT_DRAFT=null;
let TOOL_CHAT_BUSY=false;   // true while a chat turn is in flight — drives the loader
let TOOL_CHAT_AGENT='', TOOL_CHAT_MODEL='';   // '' = use the [harn] default
function renderToolChatPanel(){
  const msgs=TOOL_CHAT_HISTORY.map(h=>
    `<div class="ds"><b>${h.role==='user'?'You':'Agent'}:</b> ${esc(h.text)}</div>`).join('');
  const thinking=TOOL_CHAT_BUSY
    ? `<div class="thinking"><span class="spinner"></span> Agent is thinking…</div>` : '';
  const draftHtml=TOOL_CHAT_DRAFT
    ? `<div class="skillrow"><div style="width:100%">`+
      `<div class="nm">${esc(TOOL_CHAT_DRAFT.name)}</div>`+
      `<div class="ds">${esc(TOOL_CHAT_DRAFT.description)}</div>`+
      `<div class="ds">params: ${esc((TOOL_CHAT_DRAFT.params||[]).join(', ')||'(none)')}</div>`+
      `<div class="ds">command: ${esc(TOOL_CHAT_DRAFT.command)}</div>`+
      `<button onclick="saveToolDraft()">Save</button></div></div>`
    : `<div class="empty">No draft yet — describe the tool below.</div>`;
  const agentOpts=allAgentNames();
  const modelOpts=(agentCaps(TOOL_CHAT_AGENT||MODELS.default_agent||'').models)||[];
  return `<h2 style="margin-top:18px">DESCRIBE A NEW TOOL TO THE AGENT</h2>`+
    `<div class="row" style="gap:8px;margin-bottom:8px">`+
    `<select onchange="setToolChatAgent(this.value)" title="Agent for this drafting chat">`+
    `<option value="">(default: ${esc(MODELS.default_agent||'—')})</option>`+
    agentOpts.map(a=>`<option value="${esc(a)}" ${TOOL_CHAT_AGENT===a?'selected':''}>${esc(AGENT_LABEL[a]||a)}</option>`).join('')+
    `</select>`+
    `<select onchange="TOOL_CHAT_MODEL=this.value" title="Model for this drafting chat" ${modelOpts.length?'':'disabled'}>`+
    `<option value="">(default model)</option>`+
    modelOpts.map(m=>`<option value="${esc(m)}" ${TOOL_CHAT_MODEL===m?'selected':''}>${esc(m)}</option>`).join('')+
    `</select></div>`+
    `<div id="toolChatMsgs">${msgs}${thinking}</div>`+
    `<textarea id="toolChatInput" rows="2" placeholder="What should this tool do?" ${TOOL_CHAT_BUSY?'disabled':''}></textarea>`+
    `<button onclick="sendToolChat()" ${TOOL_CHAT_BUSY?'disabled':''}>${TOOL_CHAT_BUSY?'Thinking…':'Send'}</button>`+
    `<h4>Draft</h4>${draftHtml}`;
}
function setToolChatAgent(a){
  TOOL_CHAT_AGENT=a;
  const valid=(agentCaps(a||MODELS.default_agent||'').models)||[];
  if(TOOL_CHAT_MODEL && valid.length && !valid.includes(TOOL_CHAT_MODEL)) TOOL_CHAT_MODEL='';
  renderTools();
}
async function sendToolChat(){
  if(TOOL_CHAT_BUSY)return;
  const input=$('#toolChatInput');
  const message=input.value.trim();
  if(!message)return;
  TOOL_CHAT_HISTORY.push({role:'user', text:message});
  input.value='';
  TOOL_CHAT_BUSY=true;
  renderTools();
  try{
    const r=await post_('/api/tools/chat',{history:TOOL_CHAT_HISTORY.slice(0,-1), message,
      agent:TOOL_CHAT_AGENT, model:TOOL_CHAT_MODEL});
    TOOL_CHAT_HISTORY.push({role:'agent', text:r.reply||''});
    if(r.draft) TOOL_CHAT_DRAFT=r.draft;
  } finally {
    TOOL_CHAT_BUSY=false;
    renderTools();
  }
}
async function saveToolDraft(){
  if(!TOOL_CHAT_DRAFT)return;
  const r=await post_('/api/tools/save',{
    name:TOOL_CHAT_DRAFT.name, description:TOOL_CHAT_DRAFT.description,
    params:TOOL_CHAT_DRAFT.params, command:TOOL_CHAT_DRAFT.command, source:'chat'});
  if(!r.ok){ alert(r.error||'save failed'); return; }
  alert('Saved. This tool will be available to the agent starting its NEXT session — not the one currently running.');
  TOOL_CHAT_HISTORY=[]; TOOL_CHAT_DRAFT=null;
  await loadToolsData(true);
  renderTools();
}
function renderToolEditor(){
  const users=S.workflow.nodes.filter(n=>(n.tools||[]).includes(toolSel));
  const doc=toolDoc(toolSel);
  $('#insp').innerHTML=`
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">Tool</h2>
      <button class="icon-btn" onclick="deleteTool('${esc(toolSel)}')" title="Remove this tool from every step">${TRASH_SVG}</button>
    </div>
    <label>Name <span class="mut">(rename across all steps)</span></label>
    <input type="text" value="${esc(toolSel)}" onchange="renameTool('${esc(toolSel)}',this.value)"/>
    <label>What it does · why it's needed · when to use it</label>
    <div class="toolDoc">${esc(doc)}</div>
    <label>Used by ${users.length} step(s)</label>
    <div class="skillgrid">${users.map(u=>`<span class="chip" style="cursor:pointer" onclick="goToNode('${esc(u.title)}')">${esc(u.title)}</span>`).join('')||'<span class="mut">none</span>'}</div>
    <div class="mut" style="margin-top:14px">Tools are part of the workflow — click <b>Save flow</b> on the Flow tab to persist renames/removals.</div>`;
}
function renameTool(oldn,newn){ newn=(newn||'').trim(); if(!newn||newn===oldn){renderTools();return;}
  S.workflow.nodes.forEach(n=>{ if(n.tools){ const k=n.tools.indexOf(oldn); if(k>=0)n.tools[k]=newn; } });
  toolSel=newn; checkDirty(); renderTools(); }
function deleteTool(t){ if(!confirm('Remove tool "'+t+'" from all steps?'))return;
  S.workflow.nodes.forEach(n=>{ if(n.tools){ const k=n.tools.indexOf(t); if(k>=0)n.tools.splice(k,1); } });
  toolSel=null; checkDirty(); renderTools(); }

/* ---------- resizable inspector ---------- */
let rs=null;
$('#grip').addEventListener('pointerdown',e=>{
  rs={sx:e.clientX,w:parseInt(getComputedStyle(document.documentElement).getPropertyValue('--insp-w'))};
  $('#grip').classList.add('act'); $('#grip').setPointerCapture(e.pointerId); e.preventDefault();
});
window.addEventListener('pointermove',e=>{
  if(!rs)return;
  let w=rs.w-(e.clientX-rs.sx); w=Math.max(300,Math.min(820,w));
  document.documentElement.style.setProperty('--insp-w',w+'px');
});
window.addEventListener('pointerup',()=>{ if(rs){ $('#grip').classList.remove('act'); rs=null; } });

function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
window.addEventListener('beforeunload',e=>{ if(dirty){e.preventDefault();e.returnValue='';} });
load();
</script>
</body>
</html>
"""
