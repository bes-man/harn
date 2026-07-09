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
from . import tasks as tasks_mod
from . import workflow as workflow_mod
from . import workflows as workflows_mod
from .config import Config


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
    return {"tools": {name: tool_notes.merged(name, doc)
                      for name, doc in catalog.items()}}


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
    tasks_mod._save(t)
    return {"ok": True, "task_id": t.id, "workflow": t.workflow}


def task_plan_payload(env_dir: Path, task_id: str) -> dict:
    """The task's OWN workflow plan (harn_env/tasks/<id>.workflow.json) for the
    Board's "Edit this task's plan" button — the same Flow canvas component
    used for presets, just pointed at a per-task snapshot instead."""
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.snapshot_for_task(env_dir, task_id, task.workflow)
    if plan is None:
        return {"ok": False, "error": "no plan"}
    return {"ok": True, "plan": plan, "task_id": task_id}


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
    gitutil.checkpoint / loop.run_step), discarding its last attempt."""
    task_id = (payload.get("task_id") or "").strip()
    step_id = (payload.get("step_id") or "").strip()
    task = tasks_mod.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task {task_id}"}
    plan = workflows_mod.load_task_plan(env_dir, task_id) \
        or workflows_mod.snapshot_for_task(env_dir, task_id, task.workflow)
    known = {n.get("id") for n in plan.get("nodes", []) if n.get("kind") == "step"}
    if step_id not in known:
        return {"ok": False, "error": f"unknown step '{step_id}'"}
    return runner_mod.launch(env_dir.parent, env_dir, task_id,
                             step=step_id, rerun=bool(payload.get("rerun")))


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

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
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
            elif route == "/api/models":
                self._json(models_payload(env))
            elif route == "/api/task_plan":
                self._json(task_plan_payload(env, self._query("task") or ""))
            elif route == "/api/attachments/file":
                task_id, name = self._query("task"), self._query("name")
                data = attachments_mod.read_bytes(env, task_id, name)
                if data is None:
                    self._send(404, b"not found", "text/plain"); return
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self._send(200, data, ctype)
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
            elif route == "/api/layout":
                self._json(apply_layout(env, body))
            elif route == "/api/config":
                self._json(set_config_flag(env, body))
            elif route == "/api/tasks/workflow":
                self._json(set_task_workflow(env, body))
            elif route == "/api/tasks/launch":
                self._json(launch_task(env, body))
            elif route == "/api/tasks/stop":
                self._json(stop_task(env, body))
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
  .node .tools{margin-top:7px;font-size:11px;color:var(--muted);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
  .node .stat{margin-top:7px;font-size:10.5px;color:var(--muted);
    font-family:ui-monospace,Menlo,monospace;border-top:1px solid var(--line);padding-top:5px}
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
  <div class="insp" id="insp"><div class="empty">Select a node to edit it.</div></div>
</main>
<script>
const $=s=>document.querySelector(s);
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
async function load(){
  const r=await fetch(api('/api/state')); S=await r.json();
  L=Object.assign({}, S.layout||{});
  snapshotWorkflow(); clearDirty();
  setStatus(S.skills.length+' skills · '+S.workflow.nodes.filter(n=>n.kind==='step').length+' steps');
  if(!Object.keys(TOOL_DOCS).length){
    try{ TOOL_DOCS=(await (await fetch(api('/api/tools'))).json()).tools||{}; }catch(e){}
  }
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
async function pollProgress(){
  try{ PROG=await (await fetch(api('/api/progress'))).json(); }catch(e){ return; }
  if(tab==='flow'){
    applyProgress();
    // Update the terminal block in place (not a full renderFlow rebuild) so
    // polling every 1.5s never disrupts a drag or steals focus elsewhere.
    const term=document.querySelector('.node.terminal');
    if(term) renderFlowTerminal(term);
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
  renderBoard();
  if(boardSel&&(BOARD.tasks||[]).some(t=>t.id===boardSel)) renderTaskDetail();
  else{ boardSel=null; $('#insp').innerHTML='<div class="empty">Select a task.</div>'; }
}
function selectTask(id){ boardSel=id; renderBoard(); renderTaskDetail(); }
function renderBoard(){
  const v=$('#listView');
  const groups={}; (BOARD.tasks||[]).forEach(t=>(groups[t.status]=groups[t.status]||[]).push(t));
  let html='<h2>BOARD</h2>';
  if(BOARD.run){
    const rt=(BOARD.tasks||[]).find(t=>t.id===BOARD.run.task_id);
    html+=`<div class="runbanner">▶ running <b>${esc(BOARD.run.task_id)}</b>`+
      `${rt?': '+esc(rt.title):''} (pid ${BOARD.run.pid}${BOARD.run.auto?' · auto':''})`+
      `<button class="ghost" onclick="stopRun()">■ Stop</button></div>`;
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
      <span class="statusbadge" style="color:${statusColor};border-color:${statusColor}">${esc(t.status)}</span>
    </div>
    <div class="taskTitle">${esc(t.title)}</div>
    <label>Workflow <span class="mut">(what the agent follows when this task runs)</span></label>
    <select onchange="assignWorkflow('${esc(t.id)}',this.value)">${wfOpts}</select>
    <div class="row" style="margin-top:8px">
      <button class="ghost" onclick="openTaskPlan('${esc(t.id)}')" title="Open this task's own copy of its plan — edits affect only this task">✎ Edit this task's plan</button>
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
function setStepField(field,val){
  if(!selNode) return;
  selNode[field]=(val||'').trim();
  if(field==='agent'){
    const valid=(agentCaps(selNode.agent||MODELS.default_agent||'').models)||[];
    if(selNode.model && valid.length && !valid.includes(selNode.model)) selNode.model='';
  }
  checkDirty(); renderInsp();
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
    oninput="setStepField('${field}',this.value)"/>`;
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
  const runnable=flowAllTasks().find(t=>RUNNABLE_STATUSES.includes(t.status));
  const first=runnable||flowAllTasks()[0];
  return first?first.id:null;
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
    <select id="flowTaskSel" onchange="renderFlow()">${opts}</select>
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
  await pollBoard(); renderFlow();
}
function skillNames(){ return S.skills.map(s=>s.name); }
function allTools(){ const s=new Set(); S.workflow.nodes.forEach(n=>(n.tools||[]).forEach(t=>s.add(t))); return [...s].sort(); }
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
    const reqChips=(n.required||[]).map(s=>`<span class="chip req">${esc(s)}</span>`).join('');
    const tools=(n.tools||[]).length?`<div class="tools">⚙ ${(n.tools||[]).map(esc).join(', ')}</div>`:'';
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
    el.innerHTML=`${stepBtns}${onoff}<div class="ttl"><span class="num">${esc(num)}</span><span>${esc(n.title)}</span></div>
      <div class="chips">${reqChips|| (n.kind==='step'?'<span class="chip">no required skills</span>':'')}</div>${tools}`;
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
  fitSurface(); redrawEdges();
  applyProgress();
  renderInsp();
  renderPlanBanner();
}
function fitSurface(){
  let mx=1200,my=900;
  document.querySelectorAll('.node').forEach(e=>{
    mx=Math.max(mx,e.offsetLeft+e.offsetWidth+200);
    my=Math.max(my,e.offsetTop+e.offsetHeight+200);
  });
  const s=$('#surface'); s.style.width=mx+'px'; s.style.height=my+'px';
}
function redrawEdges(){
  const nodes=[...document.querySelectorAll('.node')].sort((a,b)=>a.dataset.i-b.dataset.i);
  let d='';
  for(let i=0;i<nodes.length-1;i++){
    const a=nodes[i], b=nodes[i+1];
    const x1=a.offsetLeft+a.offsetWidth/2, y1=a.offsetTop+a.offsetHeight;
    const x2=b.offsetLeft+b.offsetWidth/2, y2=b.offsetTop;
    const dy=Math.max(30,Math.abs(y2-y1)/2);
    d+=`<path class="edge" d="M${x1} ${y1} C ${x1} ${y1+dy} ${x2} ${y2-dy} ${x2} ${y2}"/>`
      +`<circle cx="${x2}" cy="${y2}" r="3" fill="#3a4150"/>`;
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
    id:'',agent:'',model:'',effort:'',temperature:'',type:'',command:'',on_fail:''};
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
    if(drag.moved){ resortByPosition(); renderFlow(); saveLayout(); checkDirty(); } // reorder → renumber
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
    return `<span class="tog ${on?'on':''}" onclick="toggleReq('${esc(name)}')">${esc(name)}</span>`;
  }).join('');
  const reqLinks=(n.required||[]).map(name=>`<a class="link" onclick="editSkill('${esc(name)}')">edit ${esc(name)} »</a>`).join(' · ');
  const toolTogs=allTools().map(t=>{
    const on=(n.tools||[]).includes(t);
    return `<span class="tog ${on?'on':''}" title="${esc(toolDoc(t))}" onclick="toggleTool('${esc(t)}')">${esc(t)}</span>`;
  }).join('');
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
      oninput="setStepField('command',this.value)" placeholder="npm test">${esc(n.command||'')}</textarea>
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
    </div>${runBtns}${runNote}`;
  })():'';
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
    <label>Tools at this step <span class="mut">(click to toggle · add below)</span></label>
    <div class="skillgrid">${toolTogs||'<span class="mut">no tools yet</span>'}</div>
    <input type="text" placeholder="add a tool, press Enter" style="margin-top:8px"
      onkeydown="if(event.key==='Enter'){addTool(this.value);this.value='';}"/>
    ${typeToggle}${stepType==='agent'?modelSection:commandSection}`:''}
  `;
}
function upd(k,v){
  if(!selNode) return; const old=selNode.title;
  selNode[k]=v; checkDirty();
  if(k==='title'){ if(L[old]){ L[v]=L[old]; if(v!==old) delete L[old]; } renderFlow(); }
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
let toolSel=null;
function renderTools(){
  const v=$('#listView'); const tools=allTools();
  v.innerHTML='<h2>TOOLS <span class="mut" style="text-transform:none;letter-spacing:0">— used across steps; Save flow to persist</span></h2>';
  tools.forEach(t=>{ const users=S.workflow.nodes.filter(n=>(n.tools||[]).includes(t));
    const doc=toolDoc(t); const first=doc.split('\n')[0];
    const r=document.createElement('div');r.className='skillrow';r.title=doc;
    r.onclick=()=>{toolSel=t;renderToolEditor();};
    r.innerHTML=`<div style="width:100%"><div class="nm">${esc(t)} <span class="mut" style="font-weight:400">· ${users.length} step(s)</span></div>`+
      `<div class="ds">${esc(first)}</div></div>`;
    v.appendChild(r); });
  if(!tools.length) v.innerHTML+='<div class="empty">No tools yet — add tools on a step (Flow tab).</div>';
  if(toolSel&&tools.includes(toolSel)) renderToolEditor(); else $('#insp').innerHTML='<div class="empty">Select a tool.</div>';
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
