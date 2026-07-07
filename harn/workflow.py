"""The project's end-to-end workflow — `harn_env/WORKFLOW.md`.

A single, first-class, ALWAYS-followed file (every agent: Claude/Cursor/Codex/
Qwen). It is the canonical flow the agent walks plus, per step, the skills that
are REQUIRED (always loaded) — the agent additionally loads task-relevant skills
on demand, but never loads *every* skill (that would flood the context window;
harn's whole point is feeding the next session exactly the relevant knowledge).

Format is structured Markdown on purpose (not YAML): the agent *executes* these
steps, and an LLM follows prose far better than raw config; it stays stdlib-only
(no YAML dep); and harn still machine-reads the `Skills (required: …)` line per
step with a regex — the same trick used for skill/service frontmatter.

The file is the USER's to edit and comment (`<!-- … -->`). So:
  • `write()` CREATES it only if missing — it never clobbers user edits.
  • the agent co-authors a project-specific version during onboarding and saves
    it via the `save_workflow` MCP tool.
  • `refresh_skills()` updates ONLY the snapshot block between markers, leaving
    every hand-edited step intact (called by `harn onboard` / `harn workflow
    --refresh`).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import skills as skills_mod

FILENAME = "WORKFLOW.md"

_SNAP_START = "<!-- harn:skills-snapshot:start -->"
_SNAP_END = "<!-- harn:skills-snapshot:end -->"

# Per-step required skills are declared on a `Skills (required: a, b)` line.
# Anchored to the WHOLE line so an inline mention of the syntax inside prose
# (e.g. a note section explaining the format) is NOT mistaken for a declaration.
_REQ_RE = re.compile(r"^\s*Skills\s*\(required:\s*([^)]*)\)\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
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

_BODY = """\
# Project workflow

<!--
  This file is YOURS — edit and comment it freely. It is ALWAYS followed, by
  every agent. harn reads each step's `Skills (required: …)` line and makes sure
  those skills are loaded; the agent loads other RELEVANT skills on demand and
  never loads skills it doesn't need (keeping the context window lean is the
  point). Refresh the snapshot at the bottom with `harn workflow --refresh`.
-->

## How harn uses this file
- Always-on, agent-agnostic. Steps are prose; `Skills (required: …)` is parsed.
- Per step: load the REQUIRED skills + any skills RELEVANT to the task — never
  the whole skill set. `read_workflow` returns this file; `save_workflow` lets
  the agent rewrite it (e.g. after agreeing changes with you).

## Session start — orient
Load harn tools (`ToolSearch("harn", 30)`; they may be deferred). If
`harn_env/state/ONBOARD.md` exists and the PRD/standards are still empty, do
ONBOARDING first (read it, map the code, interview the human one question at a
time via `ask_user(skill=…)`), then co-author this WORKFLOW with them and
`save_workflow`. Otherwise start the per-task loop.
Skills (required: project, standards)
Tools: ToolSearch, read_workflow, board, get_next_task, create_task

## 1. Pick up / create the task
`get_next_task` — or, if the user asked for a change with no matching task,
`create_task` (one sentence) then `get_next_task`. Every code change is a task.
Skills (required: project)
Tools: get_next_task, create_task

## 2. Pre-task protocol (plan mode for anything non-trivial)
AS IS → TO BE → load skills (required + the domain(s) this task touches) and NAME
them → best practices (context7) → clarify via the funnel (`ask_user`,
highest-leverage question first, one at a time) → `lock_spec`.
Skills (required: standards, constraints)
Tools: list_services, read_service, list_skills, read_skill, ask_user, lock_spec

## 3. Implement
One focused change. Record non-obvious choices.
Skills (required: standards, constraints)
Tools: record_decision, set_scratchpad

## 4. Tests
`run_tests` → fix until green. Changed code but no tests → add them.
Skills (required: testing)
Tools: run_tests

## 5. Verify
Re-read each `## Done when` criterion and check it against the ACTUAL code (not
just test output). End with `VERIFY: PASS` or `VERIFY: FAIL`.
Skills (required: standards)
Tools: read_skill, ask_user

## 6. UI verify (only user-facing work)
Drive the live app and confirm the criteria in the browser.
Skills (required: ui)
Tools: (Playwright MCP), read_design

## 7. Submit for review
Skills (required: )
Tools: submit_for_review

## 8. Reconcile — grow the knowledge base
Capture what this task taught: `save_to_skill` for confident conventions,
`ask_user(skill=…)` for trade-offs, `save_service` for changed service
standards, `record_change` for the release-note line. This is how the next
session knows more than this one did.
Skills (required: standards)
Tools: reconcile_skills, save_to_skill, save_service, record_change, ask_user

## 9. Oracle + review
Oracle runs automatically (`harn watch`). Read `board()` for PASS/FAIL/DEBT and
relay it; FAIL → rework from step 3. Assemble docs anytime with
`generate_changelog`.
Skills (required: )
Tools: board, generate_changelog, explain_pipeline

## Rules that bite
- Every code-change request = a harn task — even one-liners.
- Never put a question/choice to the human as trailing prose — use the native
  `AskUserQuestion` AND `ask_user(question, skill=…)`.
- Don't shell out to `harn run` from chat (it nests a second agent).
"""

_SKILLS_HEADER = (
    "## Skills you'll likely use\n"
    "A soft snapshot — NOT a fixed set, and NOT all loaded at once. Load a step's "
    "required skills + the ones relevant to the task; use others freely "
    "(`list_skills` for the live set, `ensure_skill(domain)` if a domain has "
    "none). Refresh: `harn workflow --refresh`.\n"
)


def _skills_block(env_dir: Path) -> str:
    return f"{_SNAP_START}\n{_SKILLS_HEADER}\n{skills_mod.index(env_dir)}\n{_SNAP_END}"


def render(env_dir: Path) -> str:
    """The full default WORKFLOW.md text: the flow + a skills snapshot block."""
    return f"{_BODY}\n{_skills_block(env_dir)}\n"


def write(env_dir: Path, *, force: bool = False) -> Path:
    """Create `harn_env/WORKFLOW.md` if missing. Never clobbers user edits unless
    `force=True` (the agent customises it via `save_workflow` instead)."""
    env_dir.mkdir(parents=True, exist_ok=True)
    p = env_dir / FILENAME
    if force or not p.exists():
        p.write_text(render(env_dir), encoding="utf-8")
    return p


def refresh_skills(env_dir: Path) -> Path:
    """Update ONLY the skills snapshot block, preserving every hand-edited step.
    Creates the file from the template if it doesn't exist yet."""
    p = env_dir / FILENAME
    if not p.exists():
        return write(env_dir)
    text = p.read_text(encoding="utf-8")
    block = _skills_block(env_dir)
    if _SNAP_START in text and _SNAP_END in text:
        text = re.sub(re.escape(_SNAP_START) + r".*?" + re.escape(_SNAP_END),
                      lambda _: block, text, flags=re.DOTALL)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    p.write_text(text, encoding="utf-8")
    return p


_TOOLS_RE = re.compile(r"^\s*Tools:\s*(.*)$", re.IGNORECASE)
_DISABLED_RE = re.compile(r"^\s*Disabled:\s*true\s*$", re.IGNORECASE)
_STEP_NUM_RE = re.compile(r"^(\d+)\.\s+(.*)$")


def parse(env_dir: Path) -> dict:
    """Parse WORKFLOW.md into an editable structure for the visual editor:

        {"preamble": "<text before the first ## heading>",
         "nodes": [{"title", "body", "kind", "enabled", "required", "tools",
                    "id", "agent", "model", "effort", "temperature"}]}

    Per node:
      • `title`  — the heading WITHOUT its step number (numbers are positional and
                   regenerated by `compose`, so reordering renumbers cleanly).
      • `kind`   — "step" (a numbered pipeline step: had a number OR carries a
                   Skills/Tools/Id/Agent/Model/Effort/Temperature line) or "note"
                   (plain doc section).
      • `enabled`— False when the section carries a `Disabled: true` line.
      • `id`, `agent`, `model`, `effort`, `temperature` — from explicit
                   `Id:`/`Agent:`/`Model:`/`Effort:`/`Temperature:` lines, each
                   defaulting to "" (unset). These are never guessed from the
                   title — only set explicitly (e.g. by the studio UI), so a
                   step's execution config survives the step being renamed
                   later. `id` is the stable key for checkpoints and Run/Rerun;
                   see `ensure_ids()`.
    The `Skills (required: …)` / `Tools: …` / `Id:` / `Agent:` / `Model:` /
    `Effort:` / `Temperature:` lines are lifted into fields. A legacy
    `Stage: …` line (from the removed fixed-stage system) is recognised and
    silently discarded on parse. The auto-generated skills snapshot block is
    skipped. `compose()` is the inverse.
    """
    p = env_dir / FILENAME
    if not p.exists():
        write(env_dir)
    text = p.read_text(encoding="utf-8")
    # Drop the snapshot block — it is regenerated, never hand-edited here.
    text = re.sub(re.escape(_SNAP_START) + r".*?" + re.escape(_SNAP_END),
                  "", text, flags=re.DOTALL).rstrip() + "\n"

    lines = text.splitlines()
    preamble: list[str] = []
    nodes: list[dict] = []
    cur: dict | None = None
    body: list[str] = []

    def _flush():
        if cur is not None:
            cur["body"] = "\n".join(body).strip()
            # A node is a numbered "step" if it was numbered OR declares skills/
            # tools; otherwise it's a plain doc "note".
            cur["kind"] = "step" if (cur.pop("_num") or cur.pop("_decl")) else "note"
            nodes.append(cur)

    for line in lines:
        h = re.match(r"^##\s+(.*)$", line)
        if h:
            _flush()
            raw = h.group(1).strip()
            m = _STEP_NUM_RE.match(raw)
            cur = {"title": (m.group(2).strip() if m else raw),
                   "required": [], "tools": [], "enabled": True,
                   "id": "", "agent": "", "model": "", "effort": "",
                   "temperature": "",
                   "_num": bool(m), "_decl": False}
            body = []
            continue
        if cur is None:
            preamble.append(line)
            continue
        if _DISABLED_RE.match(line):
            cur["enabled"] = False
            continue
        req = _REQ_RE.search(line)
        if req:
            cur["_decl"] = True
            cur["required"] = [s.strip() for s in req.group(1).split(",")
                               if re.fullmatch(r"[a-z0-9_-]+", s.strip())]
            continue
        tl = _TOOLS_RE.match(line)
        if tl:
            cur["_decl"] = True
            cur["tools"] = [t.strip() for t in tl.group(1).split(",") if t.strip()]
            continue
        if _STAGE_RE.match(line):
            cur["_decl"] = True      # legacy line: swallow, don't put in body
            continue
        id_m = _ID_RE.match(line)
        if id_m:
            cur["_decl"] = True
            cur["id"] = id_m.group(1).strip()
            continue
        agent_m = _AGENT_RE.match(line)
        if agent_m:
            cur["_decl"] = True
            cur["agent"] = agent_m.group(1).strip()
            continue
        model_m = _MODEL_RE.match(line)
        if model_m:
            cur["_decl"] = True
            cur["model"] = model_m.group(1).strip()
            continue
        effort_m = _EFFORT_RE.match(line)
        if effort_m:
            cur["_decl"] = True
            cur["effort"] = effort_m.group(1).strip()
            continue
        temp_m = _TEMP_RE.match(line)
        if temp_m:
            cur["_decl"] = True
            cur["temperature"] = temp_m.group(1).strip()
            continue
        body.append(line)
    _flush()
    return {"preamble": "\n".join(preamble).strip(), "nodes": nodes}


def compose(env_dir: Path, parsed: dict) -> str:
    """Inverse of `parse()`: render the editable structure back to WORKFLOW.md.

    Step nodes are renumbered 1..N in their current order (so reordering the list
    renumbers automatically); note nodes stay unnumbered. A disabled node keeps
    its number but carries a `Disabled: true` line so the agent skips it."""
    out = [parsed.get("preamble", "").strip(), ""]
    step_no = 0
    for n in parsed.get("nodes", []):
        kind = n.get("kind", "step")
        title = n["title"].strip()
        # strip any stray leading number the editor might have left in the title
        m = _STEP_NUM_RE.match(title)
        if m:
            title = m.group(2).strip()
        if kind == "step":
            step_no += 1
            out.append(f"## {step_no}. {title}")
        else:
            out.append(f"## {title}")
        if n.get("body", "").strip():
            out.append(n["body"].strip())
        if not n.get("enabled", True):
            out.append("Disabled: true")
        if kind == "step":
            req = [s for s in n.get("required", []) if s]
            out.append(f"Skills (required: {', '.join(req)})" if req
                       else "Skills (required: )")
        tools = [t for t in n.get("tools", []) if t]
        if tools:
            out.append(f"Tools: {', '.join(tools)}")
        for key, label in (("id", "Id"), ("agent", "Agent"), ("model", "Model"),
                           ("effort", "Effort"), ("temperature", "Temperature")):
            val = str(n.get(key) or "").strip()
            if kind == "step" and val:
                out.append(f"{label}: {val}")
        out.append("")
    body = "\n".join(out).rstrip() + "\n"
    return f"{body}\n{_skills_block(env_dir)}\n"


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


def save_parsed(env_dir: Path, parsed: dict) -> Path:
    """Write WORKFLOW.md from an edited structure (visual editor → file).

    Does NOT call `ensure_ids()` — a save must not silently stamp ids onto
    steps the caller didn't touch (steps without an explicit id stay "" until
    something needs one, e.g. `harn run` calling `ensure_ids()` itself)."""
    p = env_dir / FILENAME
    p.write_text(compose(env_dir, parsed), encoding="utf-8")
    return p


def required_skills(env_dir: Path) -> dict[str, list[str]]:
    """Parse the per-step `Skills (required: …)` lines → {step heading: [skills]}.

    Machine-readable view of the workflow harn uses to ensure a step's mandatory
    skills are loaded (vs. the agent's on-demand relevant ones). Empty `()` and a
    missing file both yield no entries."""
    p = env_dir / FILENAME
    if not p.exists():
        return {}
    out: dict[str, list[str]] = {}
    heading = ""
    disabled = False
    for line in p.read_text(encoding="utf-8").splitlines():
        h = _HEADING_RE.match(line)
        if h:
            heading = h.group(1).strip()
            disabled = False
            continue
        if _DISABLED_RE.match(line):
            disabled = True
            continue
        m = _REQ_RE.search(line)
        if m and not disabled:   # a disabled step's skills aren't required
            # Only real skill slugs — ignores doc placeholders like `…`.
            sk = [s.strip() for s in m.group(1).split(",")
                  if re.fullmatch(r"[a-z0-9_-]+", s.strip())]
            if sk:
                out[heading] = sk
    return out
