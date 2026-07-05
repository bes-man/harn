"""Named workflow presets — ``harn_env/workflows/<name>.json``.

``WORKFLOW.md`` is the DEFAULT workflow: the flow a task follows when it selects
no named preset. Named presets are *alternatives* a task can opt into. Each is
the same node structure the studio canvas edits — steps + per-step required
skills + tools — plus workflow-level meta: ``name``, ``title``, ``description``,
``version``.

Cross-agent contract (Claude / Codex / Cursor)
----------------------------------------------
Every agent reads the flow from ONE fixed file: ``WORKFLOW.md`` (the MCP
``read_workflow`` tool returns it; file-reading agents open it directly). So
"run task T under workflow X" is realised by RENDERING X's nodes into
``WORKFLOW.md`` when T is picked up (`activate`). There is no second file for an
agent to discover — the indirection lives entirely on harn's side.

The default stays safe: while the default is active, ``WORKFLOW.md`` is
authoritative and a ``default.json`` cache mirrors it (so manual edits to
``WORKFLOW.md`` are preserved). Switching to a named preset snapshots the
default first; switching back renders it verbatim.

Pure stdlib JSON, mirroring the rest of harn's lean footprint. The HTTP/studio
layer and the loop both call these functions — never poke the files directly.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import workflow as workflow_mod

DEFAULT = "default"

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")


def _dir(env_dir: Path) -> Path:
    return env_dir / "workflows"


def _path(env_dir: Path, name: str) -> Path:
    return _dir(env_dir) / f"{_slug(name)}.json"


def _active_path(env_dir: Path) -> Path:
    return env_dir / "state" / "active_workflow.txt"


# --------------------------------------------------------------------------- #
# active-workflow pointer
# --------------------------------------------------------------------------- #
def active_name(env_dir: Path) -> str:
    """The workflow currently mirrored into WORKFLOW.md. "" == the default."""
    p = _active_path(env_dir)
    if p.exists():
        return _slug(p.read_text(encoding="utf-8")) or DEFAULT
    return DEFAULT


def _set_active(env_dir: Path, name: str) -> None:
    p = _active_path(env_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_slug(name) or DEFAULT, encoding="utf-8")


# --------------------------------------------------------------------------- #
# meta + serialisation
# --------------------------------------------------------------------------- #
def _norm(name: str, d: dict) -> dict:
    """A complete workflow dict with every field defaulted."""
    slug = _slug(name) or DEFAULT
    return {
        "name": slug,
        "title": (d.get("title") or "").strip() or (
            "Default" if slug == DEFAULT else slug.replace("-", " ").title()),
        "description": (d.get("description") or "").strip(),
        "version": str(d.get("version") or "1").strip(),
        "preamble": d.get("preamble") or "",
        "nodes": list(d.get("nodes") or []),
    }


def meta(env_dir: Path, name: str) -> dict:
    """Just the listing fields (no nodes) for a workflow — for the UI switcher."""
    wf = load(env_dir, name) or _norm(name, {})
    return {k: wf[k] for k in ("name", "title", "description", "version")}


def save(env_dir: Path, wf: dict) -> Path:
    """Write a workflow JSON (creating workflows/ as needed). Returns the path."""
    name = wf.get("name") or DEFAULT
    full = _norm(name, wf)
    _dir(env_dir).mkdir(parents=True, exist_ok=True)
    p = _path(env_dir, name)
    p.write_text(json.dumps(full, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def load(env_dir: Path, name: str) -> dict | None:
    slug = _slug(name) or DEFAULT
    if slug == DEFAULT:
        return _ensure_default(env_dir)
    p = _path(env_dir, slug)
    if not p.exists():
        return None
    try:
        return _norm(slug, json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None


def delete(env_dir: Path, name: str) -> bool:
    """Remove a named preset. The default can't be deleted. If the deleted one
    was active, fall back to the default."""
    slug = _slug(name)
    if not slug or slug == DEFAULT:
        return False
    p = _path(env_dir, slug)
    if p.exists():
        p.unlink()
    if active_name(env_dir) == slug:
        activate(env_dir, DEFAULT)
    return True


def list_workflows(env_dir: Path) -> list[dict]:
    """Every workflow's meta — the default first, then named presets by name."""
    _ensure_default(env_dir)
    out = [meta(env_dir, DEFAULT)]
    d = _dir(env_dir)
    if d.exists():
        for p in sorted(d.glob("*.json")):
            if p.stem == DEFAULT:
                continue
            wf = load(env_dir, p.stem)
            if wf:
                out.append({k: wf[k] for k in
                            ("name", "title", "description", "version")})
    return out


# --------------------------------------------------------------------------- #
# default <-> WORKFLOW.md mirroring
# --------------------------------------------------------------------------- #
def _ensure_default(env_dir: Path) -> dict:
    """The default workflow as a dict, seeded from WORKFLOW.md the first time."""
    p = _path(env_dir, DEFAULT)
    if p.exists():
        try:
            return _norm(DEFAULT, json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    parsed = workflow_mod.parse(env_dir)   # creates WORKFLOW.md if missing
    wf = _norm(DEFAULT, {
        "title": "Default",
        "description": "Project default workflow (mirrors WORKFLOW.md).",
        "preamble": parsed.get("preamble", ""),
        "nodes": parsed.get("nodes", []),
    })
    save(env_dir, wf)
    return wf


def _sync_default_from_md(env_dir: Path) -> dict:
    """Capture any hand edits to WORKFLOW.md into the default cache, preserving
    the default's title/description/version meta."""
    cur = _ensure_default(env_dir)
    parsed = workflow_mod.parse(env_dir)
    cur["preamble"] = parsed.get("preamble", "")
    cur["nodes"] = parsed.get("nodes", [])
    save(env_dir, cur)
    return cur


def render(env_dir: Path, wf: dict) -> Path:
    """Materialise a workflow's nodes into WORKFLOW.md (the file every agent
    reads). The skills-snapshot block is regenerated by `workflow.compose`."""
    text = workflow_mod.compose(env_dir, {
        "preamble": wf.get("preamble", ""),
        "nodes": wf.get("nodes", []),
    })
    p = env_dir / workflow_mod.FILENAME
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# the two entry points the loop + studio use
# --------------------------------------------------------------------------- #
def activate(env_dir: Path, name: str | None) -> str:
    """Make `name` (falsy/"default" → the default) the active workflow: render it
    into WORKFLOW.md so the next agent turn follows it. Idempotent. Returns the
    slug actually activated (falls back to the default for an unknown name)."""
    target = _slug(name or "") or DEFAULT
    cur = active_name(env_dir)
    # Staying on the default → WORKFLOW.md is already authoritative; leave it (and
    # the file tree) untouched. This keeps projects that never use presets exactly
    # as they are — no rewrite, no workflows/ directory created.
    if target == DEFAULT and cur == DEFAULT:
        return DEFAULT
    # Leaving the default → capture manual WORKFLOW.md edits first, so switching
    # back later restores exactly what the user had.
    if cur == DEFAULT:
        _sync_default_from_md(env_dir)
    wf = load(env_dir, target)
    if wf is None:                       # unknown preset → default, don't break the run
        target, wf = DEFAULT, _ensure_default(env_dir)
    render(env_dir, wf)
    _set_active(env_dir, target)
    return target


def save_active(env_dir: Path, parsed: dict) -> str:
    """Persist edited nodes (studio canvas) into the ACTIVE workflow's JSON and
    re-render WORKFLOW.md so the file the agent reads stays in sync."""
    name = active_name(env_dir)
    wf = load(env_dir, name) or _norm(name, {})
    wf["preamble"] = parsed.get("preamble", wf.get("preamble", ""))
    wf["nodes"] = parsed.get("nodes", [])
    save(env_dir, wf)
    render(env_dir, wf)
    return name


def save_meta(env_dir: Path, name: str, *, title: str | None = None,
              description: str | None = None, version: str | None = None,
              new_name: str | None = None) -> dict:
    """Update a workflow's meta (title/description/version) and optionally rename
    it. Returns the updated meta. The default may be re-titled but not renamed."""
    wf = load(env_dir, name) or _norm(name, {})
    if title is not None:
        wf["title"] = title.strip()
    if description is not None:
        wf["description"] = description.strip()
    if version is not None:
        wf["version"] = str(version).strip() or "1"
    slug = _slug(name) or DEFAULT
    new_slug = _slug(new_name or "")
    if new_slug and new_slug != slug and slug != DEFAULT and new_slug != DEFAULT:
        old = _path(env_dir, slug)
        wf["name"] = new_slug
        save(env_dir, wf)
        if old.exists():
            old.unlink()
        if active_name(env_dir) == slug:
            _set_active(env_dir, new_slug)
        slug = new_slug
    else:
        save(env_dir, wf)
    return {k: wf[k] for k in ("name", "title", "description", "version")}


def create(env_dir: Path, *, name: str, title: str = "", description: str = "",
           version: str = "1", copy_from: str | None = None) -> dict:
    """Create a new named preset, optionally seeded from another workflow's nodes
    (defaults to the current default's nodes). Returns its meta."""
    slug = _slug(name)
    if not slug or slug == DEFAULT:
        raise ValueError("a preset needs a non-empty name other than 'default'")
    seed = load(env_dir, copy_from or DEFAULT) or _ensure_default(env_dir)
    wf = _norm(slug, {
        "title": title or slug.replace("-", " ").title(),
        "description": description,
        "version": version,
        "preamble": seed.get("preamble", ""),
        "nodes": seed.get("nodes", []),
    })
    save(env_dir, wf)
    return {k: wf[k] for k in ("name", "title", "description", "version")}
