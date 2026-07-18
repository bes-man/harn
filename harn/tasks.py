"""Task backlog — every task is a pair of files in ``harn_env/tasks/``.

File-naming convention
----------------------
* **With Jira** (or another tracker): use the tracker key as the filename,
  e.g. ``AUTH-42.md``. The key is stored as the task ``id``.
* **Without a tracker**: ``PRJ-001.md``, ``PRJ-002.md`` …  The prefix
  (``[harn] project`` in ``harn.toml``, default ``PRJ``) is upper-cased; the
  counter increments automatically via ``next_id()``.

Lifecycle
---------
    todo → in_progress → review ⇄ changes_requested → done

File shape
----------
``<id>.md`` — the human-facing record (what a person, or a future GitHub-issue
sync, would want to read): a YAML-ish frontmatter block (id/title/status/
priority/workflow/prds/depends_on — the ONLY thing `board()` parses, so a huge
accumulated Context section never slows it down), then fixed markdown
sections in order: Description, Result, Decisions, Review log, Changelog,
Context. ``## Context`` is always LAST and append-only for the task's
lifetime — every streamed tool_result/message the agent's turn produced,
appended via a true ``open(path, "a")`` (O(1), independent of file size) —
capped per entry (~4000 chars) so a noisy tool can't flood it. The one
exception is ``compact_context`` (spec C): a workflow step opted into
``new_session`` may summarize-and-replace the not-yet-compacted raw span with
one ``### [compacted @ ...]`` entry — a full-file rewrite, acceptable because
those steps are opt-in and infrequent, unlike the append path above.

``<id>.state.json`` — engine bookkeeping the human never needs to read:
step_results (per-step usage tracking), stage_checkpoints/task_patch_refs
(git rollback refs), claimed_by/claimed_at (parallel-worker claim lock),
spec_locked, subtasks, epic, user_story, skills, scratchpad,
workflow_confirmed, baseline_ref. Exactly the role ``<id>.workflow.json``
(unchanged, stays separate — it's a structured node array edited by Studio's
visual canvas, not prose) already plays today.

Migration
---------
Old-format ``<id>.json`` files (the single-JSON-blob shape this module used
before) are upgraded lazily the first time ``load_tasks()``/``find()`` sees
them: split into the ``.md`` + ``.state.json`` pair, old file removed. No
separate migration command.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
TODO = "todo"
IN_PROGRESS = "in_progress"
REVIEW = "review"
CHANGES_REQUESTED = "changes_requested"
DONE = "done"

LIFECYCLE = [TODO, IN_PROGRESS, REVIEW, CHANGES_REQUESTED, DONE]

_NEEDS_AGENT = {TODO, IN_PROGRESS, CHANGES_REQUESTED}
_PICK_RANK   = {IN_PROGRESS: 0, CHANGES_REQUESTED: 1, TODO: 2}
_DONE_ALIASES = {"done", "complete", "completed", "accepted"}

# Cap per Context entry (matches the existing step_results.output truncation
# convention — see loop.py's `(result.text or "")[-4000:]`). A capped entry
# gets a visible marker rather than silently losing the tail of a noisy tool
# (e.g. a full test-suite dump).
CONTEXT_CAP = 4000


def _normalize_status(raw: str) -> str:
    """Normalize a status string. Preserves any non-empty status verbatim
    (custom pipelines use names outside the built-in `LIFECYCLE`, and a task
    holding a status later removed from config must stay readable — see the
    custom-board-statuses spec) — only a `_DONE_ALIASES` synonym is
    canonicalized to `DONE`, and only a blank status falls back to `TODO`."""
    s = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if s in _DONE_ALIASES:
        return DONE
    return s or TODO


def lifecycle(env_dir: Path | None) -> list[str]:
    """The ordered status pipeline for this project — the configured
    `[board]` list, or the built-in five-status `LIFECYCLE` when the project
    has no custom config (or `env_dir` is None, e.g. legacy/global callers)."""
    if env_dir is not None:
        from . import config as config_mod
        names = [s["name"] for s in config_mod.Config.load(env_dir).board_statuses]
        if names:
            return names
    return LIFECYCLE


def cap_text(text: str, limit: int = CONTEXT_CAP) -> str:
    """Truncate `text` to `limit` chars with a visible "(truncated)" marker —
    silent truncation would hide the fact that data was dropped."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------
@dataclass
class Subtask:
    id: str
    title: str
    status: str = TODO


@dataclass
class ReviewEntry:
    ts: str
    event: str
    agent:   str | None = None
    by:      str | None = None
    comment: str | None = None
    summary: str | None = None
    notes:   str | None = None
    tokens:  str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v is not None}


@dataclass
class Decision:
    """A decision the executing agent made and its rationale.

    These are the agent's *claims* — the oracle must VERIFY each one against the
    acceptance criteria and PRD, not treat it as a given. They also serve as
    lightweight continuity: the next iteration sees what was already decided.
    """
    decision:  str
    rationale: str = ""
    ts:        str = ""
    agent:     str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v}


@dataclass
class ChangeEntry:
    """A release-notes-style record of a significant change made on a task.

    Distinct from `Decision` (the *why* behind a choice) and `review_log` (the
    task's lifecycle events): a ChangeEntry is the *what shipped* — a concise
    line plus optional detail on decisions/standards it established. Append-only,
    timestamped, NEVER injected into work prompts (so it costs zero context
    tokens during execution); read on demand to assemble documentation.
    """
    ts:      str
    summary: str
    detail:  str = ""
    agent:   str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v}


@dataclass
class Task:
    id:          str
    path:        Path          # the <id>.md file
    title:       str
    status:      str
    priority:    int = 100
    prds:        list[str] = field(default_factory=list)
    epic:        str | None = None
    user_story:  str | None = None
    skills:      list[str] = field(default_factory=list)
    subtasks:    list[Subtask] = field(default_factory=list)
    description: str = ""
    # Free-text outcome summary — rendered as the markdown "## Result" section;
    # populated from `submit_for_review`/`mark_done`'s summary.
    result:      str = ""
    # Lightweight continuity (variant 2): a short free-form note the agent
    # carries to its next iteration, plus the structured decisions it has made.
    # Kept as a real field (persisted in the state.json sidecar — see module
    # docstring) rather than removed: loop.py's prompt assembly, the
    # `set_scratchpad` MCP tool and several existing tests depend on it as
    # working continuity, independent of the new Context capture.
    scratchpad:  str = ""
    decisions:   list[Decision] = field(default_factory=list)
    # Release-notes-style log of significant changes (see ChangeEntry). Governed
    # by [log] changes; append-only; doc-source, NOT injected into work prompts.
    changelog:   list[ChangeEntry] = field(default_factory=list)
    # Git commit captured when work first started — `harn rollback` returns the
    # working tree to this point to redo the task from scratch.
    baseline_ref: str = ""
    review_log:  list[ReviewEntry] = field(default_factory=list)
    # Raw text of the markdown "## Context" section (append-only capture of
    # streamed tool_result/message content — see `append_context`). Populated
    # only by a full load (`load_tasks`/`find`); empty for the lightweight
    # frontmatter-only load `board()`/`by_status()` use.
    context:     str = ""
    # Parallelism: ids this task waits on (only runnable once all are `done`),
    # and the worker that currently owns it (set on atomic claim).
    depends_on:  list[str] = field(default_factory=list)
    claimed_by:  str | None = None
    claimed_at:  str = ""
    # Clarification funnel: set once planning has narrowed the task to a
    # verified, unambiguous spec (its `## Done when` is authoritative). The
    # executor then trusts it and the full PRD is read-on-demand, not injected.
    spec_locked: bool = False
    # Named workflow preset this task runs under (workflows/<name>.json). Empty/
    # None → the project default (WORKFLOW.md). The loop renders the selected
    # workflow into WORKFLOW.md when the task is picked up, so every agent
    # (Claude/Codex/Cursor) reads the right flow from the one file they all read.
    workflow: str | None = None
    # Set True the first time the user explicitly picks a flow for this task
    # (including explicitly re-picking the default) — see workflows_mod's
    # preview_plan for why "task.workflow is set" alone isn't enough to make
    # flow selection genuinely mandatory before a task can start.
    workflow_confirmed: bool = False
    # Per-stage git checkpoints ({stage: commit-ish ref}) — a `git stash
    # create` snapshot of the working tree taken right before that stage's
    # last attempt (see gitutil.checkpoint). "Rerun this stage" restores here
    # first, so the retry starts from EXACTLY that point instead of layering
    # a new attempt on top of a half-finished previous one. Distinct from
    # `baseline_ref` (the task's very first checkpoint, for a full rerun).
    stage_checkpoints: dict = field(default_factory=dict)
    # Ordered hidden patch refs that form this task's isolated git stage.
    task_patch_refs: list[str] = field(default_factory=list)
    # Durable per-step results ledger ({step_id: {...}}) — the step-execution
    # engine's record of what happened for each step in the task's workflow
    # snapshot (harn_env/tasks/<id>.workflow.json). Keyed by step id so it
    # survives step re-ordering/renaming in the plan.
    step_results: dict = field(default_factory=dict)
    # Local mirror of an externally-tracked task (GitLab/Linear/ClickUp —
    # pull-to-local model, no sync logic yet; shape only, see the
    # custom-board-statuses spec). {"provider": ..., "id": ..., "url": ...}
    # or None for a purely local task.
    external: dict | None = None

    @property
    def done(self) -> bool:
        return self.status == DONE

    @property
    def needs_agent(self) -> bool:
        return self.status in _NEEDS_AGENT

    @property
    def in_review(self) -> bool:
        return self.status == REVIEW


# ---------------------------------------------------------------------------
# legacy (pre-refactor) single-JSON-blob parsing — used only by the migration
# path (`_load_or_migrate`) to read an old <id>.json file once.
# ---------------------------------------------------------------------------
def _from_dict(path: Path, d: dict) -> Task:
    subtasks = [
        Subtask(
            id=s.get("id", ""),
            title=s.get("title", ""),
            status=_normalize_status(s.get("status", TODO)),
        )
        for s in (d.get("subtasks") or [])
    ]
    review_log = [
        ReviewEntry(
            ts=e.get("ts", ""),
            event=e.get("event", ""),
            agent=e.get("agent"),
            by=e.get("by"),
            comment=e.get("comment"),
            summary=e.get("summary"),
            notes=e.get("notes"),
            tokens=e.get("tokens"),
        )
        for e in (d.get("review_log") or [])
    ]
    decisions = [
        Decision(
            decision=x.get("decision", ""),
            rationale=x.get("rationale", ""),
            ts=x.get("ts", ""),
            agent=x.get("agent"),
        )
        for x in (d.get("decisions") or [])
        if x.get("decision")
    ]
    changelog = [
        ChangeEntry(
            ts=c.get("ts", ""),
            summary=c.get("summary", ""),
            detail=c.get("detail", ""),
            agent=c.get("agent"),
        )
        for c in (d.get("changelog") or [])
        if c.get("summary")
    ]
    return Task(
        id=d.get("id") or path.stem,
        path=path,
        title=d.get("title") or path.stem,
        status=_normalize_status(d.get("status", TODO)),
        priority=int(d.get("priority") or 100),
        prds=list(d.get("prds") or []),
        epic=d.get("epic"),
        user_story=d.get("user_story"),
        skills=list(d.get("skills") or []),
        subtasks=subtasks,
        description=d.get("description") or "",
        result=d.get("result") or "",
        scratchpad=d.get("scratchpad") or "",
        decisions=decisions,
        changelog=changelog,
        baseline_ref=d.get("baseline_ref") or "",
        review_log=review_log,
        depends_on=list(d.get("depends_on") or []),
        claimed_by=d.get("claimed_by"),
        claimed_at=d.get("claimed_at") or "",
        spec_locked=bool(d.get("spec_locked", False)),
        workflow=d.get("workflow") or None,
        workflow_confirmed=bool(d.get("workflow_confirmed", False)),
        stage_checkpoints=dict(d.get("stage_checkpoints") or {}),
        task_patch_refs=list(d.get("task_patch_refs") or []),
        step_results=dict(d.get("step_results") or {}),
    )


def to_dict(task: Task) -> dict:
    """The full task as a JSON-safe dict (status, workflow, scratchpad, decisions,
    review_log, …) — how the studio UI's board renders task detail without
    re-deriving the shape here. (Deliberately omits the raw `context` blob —
    it can grow unboundedly, and dumping it into every board-payload response
    would reintroduce the exact cost problem this module's file split fixes;
    a scoped Context viewer is a follow-up once Studio's UI wants it.)"""
    return {
        "id":          task.id,
        "title":       task.title,
        "status":      task.status,
        "priority":    task.priority,
        "prds":        task.prds,
        "epic":        task.epic,
        "user_story":  task.user_story,
        "skills":      task.skills,
        "subtasks":    [{"id": s.id, "title": s.title, "status": s.status}
                        for s in task.subtasks],
        "description": task.description,
        "result":      task.result,
        "scratchpad":  task.scratchpad,
        "decisions":   [x.to_dict() for x in task.decisions],
        "changelog":   [c.to_dict() for c in task.changelog],
        "baseline_ref": task.baseline_ref,
        "review_log":  [e.to_dict() for e in task.review_log],
        "depends_on":  task.depends_on,
        "claimed_by":  task.claimed_by,
        "claimed_at":  task.claimed_at,
        "spec_locked": task.spec_locked,
        "workflow":    task.workflow,
        "workflow_confirmed": task.workflow_confirmed,
        "stage_checkpoints": task.stage_checkpoints,
        "task_patch_refs": task.task_patch_refs,
        "step_results": task.step_results,
        "external": task.external,
    }


# ---------------------------------------------------------------------------
# markdown + sidecar serialisation
# ---------------------------------------------------------------------------
_CONTEXT_MARKER = "## Context"
_CONTEXT_MARKER_LINE = f"\n{_CONTEXT_MARKER}\n"
_FRONTMATTER_FIELDS = ("id", "title", "status", "priority", "workflow",
                       "prds", "depends_on", "external")


def _state_path(md_path: Path) -> Path:
    return md_path.parent / f"{md_path.stem}.state.json"


def _state_dict(task: Task) -> dict:
    return {
        "epic":        task.epic,
        "user_story":  task.user_story,
        "skills":      task.skills,
        "subtasks":    [{"id": s.id, "title": s.title, "status": s.status}
                        for s in task.subtasks],
        "scratchpad":  task.scratchpad,
        "claimed_by":  task.claimed_by,
        "claimed_at":  task.claimed_at,
        "spec_locked": task.spec_locked,
        "workflow_confirmed": task.workflow_confirmed,
        "baseline_ref": task.baseline_ref,
        "stage_checkpoints": task.stage_checkpoints,
        "task_patch_refs": task.task_patch_refs,
        "step_results": task.step_results,
    }


def _render_frontmatter(task: Task) -> str:
    values = {
        "id": task.id, "title": task.title, "status": task.status,
        "priority": task.priority, "workflow": task.workflow,
        "prds": task.prds, "depends_on": task.depends_on,
        "external": task.external,
    }
    lines = ["---"]
    for k in _FRONTMATTER_FIELDS:
        lines.append(f"{k}: {json.dumps(values[k], ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter_line(line: str) -> tuple[str, object] | None:
    if ":" not in line:
        return None
    k, _, v = line.partition(":")
    k = k.strip()
    v = v.strip()
    if not k:
        return None
    try:
        return k, json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return k, v


def _read_frontmatter(path: Path) -> dict:
    """Bounded read: only the lines between the two `---` markers. Never
    touches the rest of the file — the point is that a huge accumulated
    `## Context` section costs nothing here (see module docstring)."""
    fm: dict = {}
    try:
        with path.open("r", encoding="utf-8") as f:
            first = f.readline()
            if first.strip() != "---":
                return fm
            for line in f:
                if line.strip() == "---":
                    break
                parsed = _parse_frontmatter_line(line)
                if parsed:
                    fm[parsed[0]] = parsed[1]
    except OSError:
        pass
    return fm


def _split_frontmatter(text: str) -> tuple[dict, str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    fm: dict = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        parsed = _parse_frontmatter_line(lines[i])
        if parsed:
            fm[parsed[0]] = parsed[1]
        i += 1
    body = "\n".join(lines[i + 1:]) if i < len(lines) else ""
    return fm, body.lstrip("\n")


_KNOWN_SECTIONS = ("Description", "Result", "Decisions", "Review log", "Changelog")
_SECTION_HEADER_SET = {f"## {h}" for h in _KNOWN_SECTIONS}


def _split_body(body: str) -> tuple[dict[str, str], str]:
    """Split into named sections + the raw Context blob.

    Sections are matched on an EXACT heading line (e.g. "## Description"), not
    a generic "any '## ...' line" regex — the Description/Result content
    itself routinely contains its own "## What" / "## Done when" sub-headings
    (see `_DESCRIPTION_TEMPLATE`, `lock_spec`), which would otherwise be
    mistaken for new top-level sections and corrupt the round-trip.

    Once the (always-last) "## Context" heading is seen, EVERYTHING after it
    is the raw context blob, unconditionally — no further header matching —
    so a captured tool_result that happens to contain a line looking like a
    section heading can never re-split the file.
    """
    lines = body.split("\n")
    context_idx = next((i for i, ln in enumerate(lines) if ln == _CONTEXT_MARKER), None)
    if context_idx is None:
        head_lines, context_lines = lines, []
    else:
        head_lines, context_lines = lines[:context_idx], lines[context_idx + 1:]
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in head_lines:
        if line in _SECTION_HEADER_SET:
            current = line[3:]
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    out = {k: "\n".join(v).strip("\n") for k, v in sections.items()}
    context_body = "\n".join(context_lines)
    if context_body.startswith("\n"):
        context_body = context_body[1:]
    return out, context_body


def _bullets(text: str) -> list[str]:
    return [ln[2:].strip() if ln.startswith("- ") else ln.strip()
            for ln in text.splitlines() if ln.strip()]


def _render_bullets(entries: list) -> str:
    return "\n".join(f"- {json.dumps(e.to_dict(), ensure_ascii=False)}"
                     for e in entries)


def _render_header(task: Task) -> str:
    """Everything except the (preserved-verbatim) Context body: frontmatter +
    Description + Result + Decisions + Review log + Changelog + the bare
    `## Context` heading."""
    parts = [_render_frontmatter(task), "", "## Description",
             task.description.strip(), "", "## Result", task.result.strip(),
             "", "## Decisions", _render_bullets(task.decisions),
             "", "## Review log", _render_bullets(task.review_log),
             "", "## Changelog", _render_bullets(task.changelog),
             "", _CONTEXT_MARKER]
    return "\n".join(parts) + "\n"


def _existing_context_body(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    idx = text.find(_CONTEXT_MARKER_LINE)
    if idx == -1:
        return ""
    return text[idx + len(_CONTEXT_MARKER_LINE):]


def _build_task(path: Path, fm: dict, sections: dict, context_body: str,
                state: dict) -> Task:
    decisions = [Decision(**json.loads(ln)) for ln in _bullets(sections.get("Decisions", ""))]
    review_log = [ReviewEntry(**json.loads(ln)) for ln in _bullets(sections.get("Review log", ""))]
    changelog = [ChangeEntry(**json.loads(ln)) for ln in _bullets(sections.get("Changelog", ""))]
    subtasks = [
        Subtask(id=s.get("id", ""), title=s.get("title", ""),
                status=_normalize_status(s.get("status", TODO)))
        for s in (state.get("subtasks") or [])
    ]
    return Task(
        id=fm.get("id") or path.stem,
        path=path,
        title=fm.get("title") or path.stem,
        status=_normalize_status(fm.get("status", TODO)),
        priority=int(fm.get("priority") or 100),
        prds=list(fm.get("prds") or []),
        depends_on=list(fm.get("depends_on") or []),
        workflow=fm.get("workflow") or None,
        external=fm.get("external") or None,
        description=sections.get("Description", "").strip(),
        result=sections.get("Result", "").strip(),
        decisions=decisions,
        review_log=review_log,
        changelog=changelog,
        context=context_body,
        epic=state.get("epic"),
        user_story=state.get("user_story"),
        skills=list(state.get("skills") or []),
        subtasks=subtasks,
        scratchpad=state.get("scratchpad") or "",
        claimed_by=state.get("claimed_by"),
        claimed_at=state.get("claimed_at") or "",
        spec_locked=bool(state.get("spec_locked", False)),
        workflow_confirmed=bool(state.get("workflow_confirmed", False)),
        baseline_ref=state.get("baseline_ref") or "",
        stage_checkpoints=dict(state.get("stage_checkpoints") or {}),
        task_patch_refs=list(state.get("task_patch_refs") or []),
        step_results=dict(state.get("step_results") or {}),
    )


def _read_state(md_path: Path) -> dict:
    try:
        return json.loads(_state_path(md_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_full(path: Path) -> Task:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    fm, body = _split_frontmatter(text)
    sections, context_body = _split_body(body)
    return _build_task(path, fm, sections, context_body, _read_state(path))


def _load_light(path: Path) -> Task:
    """Frontmatter + the (small, bounded) state.json sidecar only — no body
    sections, no Context. Used by `board()`/`by_status()` so an
    ever-growing Context section never shows up in, or slows down, the
    glanceable board view."""
    return _build_task(path, _read_frontmatter(path), {}, "", _read_state(path))


# Backward-compat alias: a couple of call sites (and tests/conftest.make_task)
# load a single task file directly by path.
_load = _load_full


import contextlib
import os as _os


@contextlib.contextmanager
def _claim_lock(env_dir: Path):
    """Cross-process advisory lock guarding atomic task claims. Uses fcntl where
    available (macOS/Linux); degrades to a no-op lock elsewhere so single-process
    use still works."""
    lock_dir = env_dir / "tasks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".claim.lock"
    fh = open(lock_path, "w")
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass  # no fcntl (e.g. Windows) → best-effort, single-process safe
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def _save(task: Task) -> None:
    task.path.parent.mkdir(parents=True, exist_ok=True)
    existing_context = _existing_context_body(task.path)
    task.path.write_text(_render_header(task) + existing_context, encoding="utf-8")
    _state_path(task.path).write_text(
        json.dumps(_state_dict(task), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_context(env_dir: Path, task_id: str, *, step_id: str,
                   step_title: str = "", text: str) -> None:
    """Append one captured entry to `<task_id>.md`'s `## Context` section.

    A true `open(path, "a")` — O(1) regardless of how large the file has
    grown — because this is called far more often (every streamed
    tool_result/message within a turn) than the header-rewriting mutators
    above (a few times per step). Capped at `CONTEXT_CAP` chars with a visible
    "(truncated)" marker so a noisy tool can't flood the file silently.
    """
    text = (text or "").strip()
    if not text:
        return
    path = env_dir / "tasks" / f"{task_id}.md"
    if not path.exists():
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    heading = f"### {step_id} — {step_title} ({ts})" if step_title else f"### {step_id} ({ts})"
    entry = f"\n{heading}\n{cap_text(text)}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)


# ---------------------------------------------------------------------------
# per-step new_session compaction (spec C) — a deliberate context-compaction
# boundary a workflow step can opt into. Unlike `append_context` above (a
# high-frequency O(1) append), this REWRITES the file: it replaces the raw
# span of `## Context` since the last compaction marker with one summarized
# entry. Acceptable because `new_session` steps are opt-in and infrequent.
# ---------------------------------------------------------------------------
_COMPACTED_PREFIX = "### [compacted @ "
_HEADING_RE = re.compile(r"^### .*$", re.MULTILINE)
_RAW_HEADING_ID_RE = re.compile(r"^### (\S+)")


def _last_compacted_end(context_body: str) -> int:
    """Index right after the LAST `### [compacted @ ...]` entry's full text
    (heading + summary, up to the next `### ` heading or end of string), or
    0 if there is no compacted entry yet — i.e. where the not-yet-compacted
    raw span begins."""
    end = 0
    headings = list(_HEADING_RE.finditer(context_body))
    for i, m in enumerate(headings):
        if not context_body[m.start():].startswith(_COMPACTED_PREFIX):
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(context_body)
    return end


def _entry_step_ids(raw_span: str) -> list[str]:
    """The distinct step ids of every RAW (non-compacted) entry heading in
    `raw_span`, in first-seen order — used to label a new compacted entry's
    "covers steps <ids>" line."""
    ids: list[str] = []
    for m in _HEADING_RE.finditer(raw_span):
        line = m.group(0)
        if line.startswith(_COMPACTED_PREFIX):
            continue
        idm = _RAW_HEADING_ID_RE.match(line)
        if idm and idm.group(1) not in ids:
            ids.append(idm.group(1))
    return ids


def compact_context(env_dir: Path, task_id: str, *, summarize) -> str | None:
    """Summarize the not-yet-compacted span of `<task_id>.md`'s `## Context`
    section and rewrite it in place as one compacted entry (spec C's
    `new_session` steps).

    `summarize(raw_text, step_ids) -> str` is caller-supplied (loop.py
    dispatches the actual LLM turn — this module only owns the file
    mechanics). Its exceptions are NOT caught here — a best-effort wrapper at
    the call site (mirroring the oracle/reconcile "never crash the turn"
    convention) is the caller's job, exactly like `oracle_review` catches
    around its own `adapter.run_turn` call.

    Only the span since the last `### [compacted @ ...]` marker (or from the
    very start of `## Context`, if none exists yet) is summarized — earlier
    compacted spans are left untouched, so repeated calls stay incremental
    rather than re-summarizing the whole history every time.

    Returns the newly written entry (`### [compacted @ <ts>] covers steps
    <ids>` + the summary) so the caller can inject it into a step's own
    prompt, or `None` if there was nothing new to compact (an empty raw
    span, e.g. a `new_session` step that ran before any other step captured
    context) — the file is left untouched in that case.
    """
    path = env_dir / "tasks" / f"{task_id}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    idx = text.find(_CONTEXT_MARKER_LINE)
    if idx == -1:
        return None
    head = text[:idx + len(_CONTEXT_MARKER_LINE)]
    context_body = text[idx + len(_CONTEXT_MARKER_LINE):]
    span_start = _last_compacted_end(context_body)
    raw_span = context_body[span_start:]
    if not raw_span.strip():
        return None
    step_ids = _entry_step_ids(raw_span)
    summary = (summarize(raw_span, step_ids) or "").strip()
    if not summary:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ids_label = ", ".join(step_ids) if step_ids else "(none)"
    heading = f"{_COMPACTED_PREFIX}{ts}] covers steps {ids_label}"
    entry = f"\n{heading}\n{summary}\n"
    new_context_body = context_body[:span_start] + entry
    path.write_text(head + new_context_body, encoding="utf-8")
    return entry.strip("\n")


def _load_or_migrate(json_path: Path) -> None:
    """Old single-JSON-blob task file → the new `.md` + `.state.json` pair,
    old file removed. See module docstring."""
    try:
        d = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        d = {}
    task = _from_dict(json_path, d)
    task.path = json_path.parent / f"{json_path.stem}.md"
    _save(task)
    try:
        json_path.unlink()
    except OSError:
        pass


def _migrate_legacy(env_dir: Path) -> None:
    tasks_dir = env_dir / "tasks"
    if not tasks_dir.exists():
        return
    for p in sorted(tasks_dir.glob("*.json")):
        if p.name.endswith(".workflow.json") or p.name.endswith(".state.json"):
            continue
        _load_or_migrate(p)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def load_tasks(env_dir: Path) -> list[Task]:
    tasks_dir = env_dir / "tasks"
    if not tasks_dir.exists():
        return []
    _migrate_legacy(env_dir)
    return [_load_full(p) for p in sorted(tasks_dir.glob("*.md"))]


def _load_tasks_light(env_dir: Path) -> list[Task]:
    tasks_dir = env_dir / "tasks"
    if not tasks_dir.exists():
        return []
    _migrate_legacy(env_dir)
    return [_load_light(p) for p in sorted(tasks_dir.glob("*.md"))]


def find(env_dir: Path, task_id: str) -> Task | None:
    key = task_id.strip().lower()
    for t in load_tasks(env_dir):
        if t.id.lower() == key:
            return t
    return None


def _deps_met(task: Task, by_id: dict[str, Task]) -> bool:
    """True when every id in task.depends_on exists and is done. Unknown ids are
    treated as unmet (a typo shouldn't silently unblock the task)."""
    for dep in task.depends_on:
        d = by_id.get(dep)
        if d is None or not d.done:
            return False
    return True


def _eligible(task: Task, by_id: dict[str, Task], worker: str | None) -> bool:
    """A task an agent may pick up right now: needs work, deps satisfied, and
    either unclaimed or already owned by THIS worker (resuming its own task)."""
    if not task.needs_agent or not _deps_met(task, by_id):
        return False
    if task.claimed_by and task.claimed_by != worker:
        # In-progress tasks owned by another worker are off-limits; todo tasks
        # with a stale claim are claimable again only via re-claim (below).
        return task.status != IN_PROGRESS
    return True


def next_task(
    env_dir: Path,
    exclude: set[str] | None = None,
    *,
    claim: bool = False,
    worker: str | None = None,
    only: str | None = None,
) -> Task | None:
    """Return the highest-priority runnable task.

    Dependency-aware: a task is skipped until every id in its `depends_on` is
    `done`, so independent tasks surface in parallel while chains stay ordered.

    When `claim=True`, the pick is ATOMIC under a per-project lock: the task is
    flipped to `in_progress` and stamped `claimed_by=worker` before the lock is
    released, so two concurrent workers never get the same task. Pass a stable
    `worker` id (one per agent/process) so a worker can resume its own task.

    `only` restricts the pool to a single task id — used by `harn run --task`
    (the studio UI's per-task Launch button) so the loop works ONLY that task
    and stops once it's done/blocked/review, instead of picking up whatever is
    highest priority next.
    """
    skip = exclude or set()

    def _pick(tasks: list[Task]) -> Task | None:
        by_id = {t.id: t for t in tasks}
        pending = [t for t in tasks
                   if t.id not in skip and _eligible(t, by_id, worker)
                   and (only is None or t.id == only)]
        if not pending:
            return None
        # Prefer this worker's own in-progress task, then priority order.
        pending.sort(key=lambda t: (_PICK_RANK.get(t.status, 9), t.priority, t.id))
        return pending[0]

    if not claim:
        return _pick(load_tasks(env_dir))

    with _claim_lock(env_dir):
        tasks = load_tasks(env_dir)        # re-read inside the lock
        chosen = _pick(tasks)
        if chosen is None:
            return None
        if chosen.status == TODO:
            chosen.status = IN_PROGRESS
        chosen.claimed_by = worker
        chosen.claimed_at = _now_iso()
        _save(chosen)
        return chosen


def runnable_tasks(env_dir: Path) -> list[Task]:
    """All tasks that could be started RIGHT NOW by some worker: needs work,
    deps satisfied, and not already claimed by another worker. The count is the
    available parallelism — N>1 means N independent tasks can run at once."""
    all_tasks = load_tasks(env_dir)
    by_id = {t.id: t for t in all_tasks}
    out = [t for t in all_tasks if _eligible(t, by_id, worker=None)]
    out.sort(key=lambda t: (t.priority, t.id))
    return out


def release_task(env_dir: Path, task_id: str, worker: str | None = None) -> bool:
    """Drop a worker's claim on a task (e.g. it's giving up / handing off).
    Only the owning worker may release. Returns True if released."""
    with _claim_lock(env_dir):
        t = find(env_dir, task_id)
        if t is None or (worker is not None and t.claimed_by != worker):
            return False
        t.claimed_by = None
        t.claimed_at = ""
        _save(t)
        return True


def tasks_in_review(env_dir: Path) -> list[Task]:
    return [t for t in load_tasks(env_dir) if t.in_review]


def by_status(env_dir: Path) -> dict[str, list[Task]]:
    out: dict[str, list[Task]] = {s: [] for s in lifecycle(env_dir)}
    for t in sorted(_load_tasks_light(env_dir), key=lambda t: (t.priority, t.id)):
        out.setdefault(t.status, []).append(t)
    return out


# ---------------------------------------------------------------------------
# id generation
# ---------------------------------------------------------------------------
_ID_COUNTER_RE = re.compile(r"^(.+?)-(\d+)$")


def next_id(env_dir: Path, prefix: str = "PRJ") -> str:
    """Return the next ``PREFIX-NNN`` id that doesn't exist yet."""
    prefix = prefix.strip().upper()
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.IGNORECASE)
    max_n = 0
    for t in load_tasks(env_dir):
        m = pat.match(t.id)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}-{max_n + 1:03d}"


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
_DESCRIPTION_TEMPLATE = (
    "## What\n{what}\n\n"
    "## Done when\n- \n\n"
    "## Notes for future agents\n"
)


def create_task(
    env_dir: Path,
    title: str,
    *,
    description: str = "",
    prds: list[str] | None = None,
    priority: int = 10,
    skills: list[str] | None = None,
    epic: str | None = None,
    user_story: str | None = None,
    task_id: str | None = None,
    id_prefix: str = "PRJ",
    depends_on: list[str] | None = None,
    workflow: str | None = None,
) -> Task:
    """Create a new task (`<id>.md` + `<id>.state.json`) and return the Task."""
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "tasks").mkdir(exist_ok=True)
    tid = task_id or next_id(env_dir, id_prefix)
    path = env_dir / "tasks" / f"{tid}.md"
    task = Task(
        id=tid,
        path=path,
        title=title,
        status=TODO,
        priority=priority,
        prds=list(prds or []),
        epic=epic,
        user_story=user_story,
        skills=list(skills or []),
        description=description or _DESCRIPTION_TEMPLATE.format(what=title),
        depends_on=list(depends_on or []),
        workflow=(workflow or None),
    )
    _save(task)
    return task


# ---------------------------------------------------------------------------
# mutations (all round-trip through _save)
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_status(task: Task, status: str, env_dir: Path | None = None) -> None:
    """Set `task.status`, validated against the project's configured
    pipeline (`lifecycle(env_dir)`) when `env_dir` is given, else the
    built-in `LIFECYCLE` (back-compat for internal call sites that only
    ever pass one of the five hardcoded statuses)."""
    allowed = lifecycle(env_dir) if env_dir is not None else LIFECYCLE
    if status not in allowed:
        raise ValueError(f"unknown status: {status!r}")
    task.status = status
    _save(task)


def submit_for_review(task: Task, agent: str, summary: str = "", tokens: str = "") -> None:
    if summary:
        task.result = summary
    task.review_log.append(ReviewEntry(
        ts=_now_iso(), event="submitted_for_review", agent=agent,
        summary=summary or None, tokens=tokens or None,
    ))
    set_status(task, REVIEW)


def request_changes(task: Task, comment: str, by: str = "user") -> None:
    task.review_log.append(ReviewEntry(
        ts=_now_iso(), event="changes_requested", by=by, comment=comment.strip(),
    ))
    set_status(task, CHANGES_REQUESTED)


def accept(task: Task, notes: str = "", by: str = "user") -> None:
    task.review_log.append(ReviewEntry(
        ts=_now_iso(), event="accepted", by=by, notes=notes.strip() or None,
    ))
    task.scratchpad = ""   # transient note is no longer needed; decisions kept
    set_status(task, DONE)


def mark_done(task: Task, summary: str = "") -> None:
    if summary:
        task.result = summary
        task.review_log.append(ReviewEntry(
            ts=_now_iso(), event="done", summary=summary,
        ))
    set_status(task, DONE)


def lock_spec(task: Task, done_when: str, approach: str = "",
              decisions: list[tuple[str, str]] | None = None) -> None:
    """Funnel result: write the narrowed, verified spec into the task and mark
    it spec-locked. `done_when` becomes the authoritative `## Done when` block;
    `approach` (optional) records the chosen implementation direction; each
    (decision, rationale) is appended to the task's decision log."""
    body = f"## What\n{task.title}\n\n## Done when\n{done_when.rstrip()}\n"
    if approach.strip():
        body += f"\n## Approach (locked)\n{approach.rstrip()}\n"
    task.description = body
    for dec, why in (decisions or []):
        if dec.strip():
            task.decisions.append(Decision(
                decision=dec.strip(), rationale=why.strip(), ts=_now_iso(),
                agent="planner"))
    task.spec_locked = True
    _save(task)


def log_started(task: Task, agent: str, rework: bool = False) -> None:
    task.review_log.append(ReviewEntry(
        ts=_now_iso(),
        event="rework_started" if rework else "started",
        agent=agent,
    ))
    _save(task)


def set_scratchpad(task: Task, notes: str) -> None:
    """Replace the agent's continuity note (carried to its next iteration).

    Locked (read-modify-write, under `_claim_lock`): two parallel-wave agents
    could otherwise both read a stale copy of `task`, each overwrite
    `scratchpad` independently, and the second `_save` silently discard
    whatever the first one wrote elsewhere on the task (e.g. a concurrently
    appended decision). Re-loading from disk INSIDE the lock (not trusting the
    caller's possibly-stale `task` object) is what actually closes the race —
    locking only the final write, with the read left outside, would not.
    """
    text = notes.strip()
    env_dir = task.path.parent.parent
    with _claim_lock(env_dir):
        fresh = _load_full(task.path)
        fresh.scratchpad = text
        _save(fresh)
        task.scratchpad = text


def record_decision(task: Task, decision: str, rationale: str = "",
                    agent: str | None = None) -> None:
    """Append a decision the agent made. Oracle will VERIFY it, not assume it.

    Locked (read-modify-write): see `set_scratchpad` docstring — the fix is
    the same shape (reload fresh under the lock, mutate, save, then sync the
    caller's in-memory `task` so callers reading `task.decisions` right after
    see the true post-write state).
    """
    if not decision.strip():
        return
    entry = Decision(
        decision=decision.strip(),
        rationale=rationale.strip(),
        ts=_now_iso(),
        agent=agent,
    )
    env_dir = task.path.parent.parent
    with _claim_lock(env_dir):
        fresh = _load_full(task.path)
        fresh.decisions.append(entry)
        _save(fresh)
        task.decisions = fresh.decisions


def record_change(task: Task, summary: str, detail: str = "",
                  agent: str | None = None) -> None:
    """Append a release-notes-style change entry (what shipped + optional
    decisions/standards). Append-only; timestamped; used to assemble docs.

    Locked (read-modify-write): same race/fix shape as `set_scratchpad` and
    `record_decision` above — two agents finishing parallel steps of the same
    task could each call this within the same instant.
    """
    if not summary.strip():
        return
    entry = ChangeEntry(
        ts=_now_iso(),
        summary=summary.strip(),
        detail=detail.strip(),
        agent=agent,
    )
    env_dir = task.path.parent.parent
    with _claim_lock(env_dir):
        fresh = _load_full(task.path)
        fresh.changelog.append(entry)
        _save(fresh)
        task.changelog = fresh.changelog


def clear_scratchpad(task: Task) -> None:
    """Clear the transient note once a task is accepted (decisions are kept)."""
    if task.scratchpad:
        task.scratchpad = ""
        _save(task)


def set_baseline(task: Task, ref: str) -> None:
    """Record the git commit work started from (for `harn rollback`)."""
    if ref and not task.baseline_ref:
        task.baseline_ref = ref
        _save(task)


# ---------------------------------------------------------------------------
# board
# ---------------------------------------------------------------------------
_STATUS_LABEL = {
    TODO:              "📋 todo",
    IN_PROGRESS:       "🔧 in progress",
    REVIEW:            "👀 review",
    CHANGES_REQUESTED: "✏️  changes requested",
    DONE:              "✅ done",
}


def board(env_dir: Path) -> str:
    """Glanceable view of every task on the track.

    Reads only each task's frontmatter + its small state.json sidecar (see
    `_load_tasks_light`) — an accumulated `## Context` section of any size
    never appears in, or slows down, this view.
    """
    groups = by_status(env_dir)
    if not any(groups.values()):
        return "(no tasks)"
    lines: list[str] = []
    by_id = {x.id: x for x in _load_tasks_light(env_dir)}
    # Configured pipeline first, then any status still holding tasks that
    # fell outside it (e.g. a status removed from `harn.toml` after tasks
    # were already set to it) — those must stay visible, never dropped.
    ordered = list(lifecycle(env_dir))
    ordered += [s for s in groups if s not in ordered]
    for status in ordered:
        items = groups.get(status) or []
        if not items:
            continue
        lines.append(_STATUS_LABEL.get(status, f"• {status}"))
        for t in items:
            prds = f" · {', '.join(t.prds)}" if t.prds else ""
            subs = f" [{sum(1 for s in t.subtasks if s.status == DONE)}/{len(t.subtasks)} subtasks]" if t.subtasks else ""
            if t.depends_on:
                unmet = [d for d in t.depends_on
                         if d not in by_id or not by_id[d].done]
                deps = (f" ⛔ waits on {', '.join(unmet)}" if unmet
                        else f" ✓ after {', '.join(t.depends_on)}")
            else:
                deps = ""
            owner = f" 👷 {t.claimed_by}" if t.claimed_by else ""
            lines.append(
                f"  - [{t.id}{prds}] {t.title} (priority {t.priority})"
                f"{subs}{deps}{owner}")
    return "\n".join(lines)


def render_changelog(env_dir: Path, task_id: str | None = None) -> str:
    """Assemble structured documentation from tasks' changelog + decisions.

    One section per task (newest changes last), each entry timestamped — the
    raw material for release notes / a CHANGELOG. `task_id` limits it to one
    task; otherwise every task with logged changes or decisions is included.
    """
    all_tasks = load_tasks(env_dir)
    if task_id:
        key = task_id.strip().lower()
        all_tasks = [t for t in all_tasks if t.id.lower() == key]
    sections: list[str] = []
    for t in all_tasks:
        if not t.changelog and not t.decisions:
            continue
        head = f"## {t.id} — {t.title}"
        meta = _STATUS_LABEL.get(t.status, t.status).strip()
        block = [head, f"_Status: {meta}_", ""]
        if t.changelog:
            block.append("### Changes")
            for c in t.changelog:
                stamp = f"{c.ts} · " if c.ts else ""
                block.append(f"- {stamp}{c.summary}")
                if c.detail:
                    for ln in c.detail.splitlines():
                        block.append(f"  {ln}")
            block.append("")
        if t.decisions:
            block.append("### Decisions")
            for d in t.decisions:
                stamp = f"{d.ts} · " if d.ts else ""
                why = f" — {d.rationale}" if d.rationale else ""
                block.append(f"- {stamp}{d.decision}{why}")
            block.append("")
        sections.append("\n".join(block).rstrip())
    if not sections:
        return "(no changes logged yet)"
    return "# Changelog\n\n" + "\n\n".join(sections) + "\n"
