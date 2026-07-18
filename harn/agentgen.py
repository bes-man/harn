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
