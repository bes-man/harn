"""Task backlog — every task is a JSON file in ``harn_env/tasks/``.

File-naming convention
----------------------
* **With Jira** (or another tracker): use the tracker key as the filename,
  e.g. ``AUTH-42.json``.  The key is stored as the task ``id``.
* **Without a tracker**: ``PRJ-001.json``, ``PRJ-002.json`` …  The prefix
  (``[harn] project`` in ``harn.toml``, default ``PRJ``) is upper-cased; the
  counter increments automatically via ``next_id()``.

Lifecycle
---------
    todo → in_progress → review ⇄ changes_requested → done

JSON schema (all fields)
------------------------
{
  "id":          "PRJ-001",          # = filename stem; unique across the project
  "title":       "Add JWT auth",
  "status":      "todo",
  "priority":    1,                  # lower = sooner

  "prds":        ["auth"],           # parent PRD slugs (≥1; tasks can span PRDs)
  "epic":        null,               # optional: Jira/tracker Epic key
  "user_story":  null,               # optional: parent Story key

  "skills":      ["security"],       # hint for the executor: which skills to load

  "subtasks": [
    {"id": "PRJ-001-1", "title": "POST /login endpoint", "status": "done"},
    {"id": "PRJ-001-2", "title": "Add /refresh endpoint", "status": "todo"}
  ],

  "description": "## What\\n...\\n\\n## Done when\\n- criterion",

  "review_log": [
    {"ts": "2026-06-01T09:14Z", "agent": "claude", "event": "started"},
    {"ts": "2026-06-01T09:31Z", "agent": "claude", "event": "submitted_for_review",
     "summary": "implemented /login + middleware", "tokens": "4200 (~$0.04)"},
    {"ts": "2026-06-01T10:02Z", "event": "changes_requested",
     "by": "user", "comment": "make expiry 15m"},
    {"ts": "2026-06-01T11:12Z", "event": "accepted",
     "by": "user", "notes": "Access tokens 15m…"}
  ]
}
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


def _normalize_status(raw: str) -> str:
    s = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return DONE if s in _DONE_ALIASES else (s if s in LIFECYCLE else TODO)


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
class Task:
    id:          str
    path:        Path
    title:       str
    status:      str
    priority:    int = 100
    prds:        list[str] = field(default_factory=list)
    epic:        str | None = None
    user_story:  str | None = None
    skills:      list[str] = field(default_factory=list)
    subtasks:    list[Subtask] = field(default_factory=list)
    description: str = ""
    # Lightweight continuity (variant 2): a short free-form note the agent
    # carries to its next iteration, plus the structured decisions it has made.
    scratchpad:  str = ""
    decisions:   list[Decision] = field(default_factory=list)
    # Git commit captured when work first started — `harn rollback` returns the
    # working tree to this point to redo the task from scratch.
    baseline_ref: str = ""
    review_log:  list[ReviewEntry] = field(default_factory=list)

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
# serialisation
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
        scratchpad=d.get("scratchpad") or "",
        decisions=decisions,
        baseline_ref=d.get("baseline_ref") or "",
        review_log=review_log,
    )


def _to_dict(task: Task) -> dict:
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
        "scratchpad":  task.scratchpad,
        "decisions":   [x.to_dict() for x in task.decisions],
        "baseline_ref": task.baseline_ref,
        "review_log":  [e.to_dict() for e in task.review_log],
    }


def _save(task: Task) -> None:
    task.path.write_text(
        json.dumps(_to_dict(task), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load(path: Path) -> Task:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        d = {}
    return _from_dict(path, d)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def load_tasks(env_dir: Path) -> list[Task]:
    tasks_dir = env_dir / "tasks"
    if not tasks_dir.exists():
        return []
    return [_load(p) for p in sorted(tasks_dir.glob("*.json"))]


def find(env_dir: Path, task_id: str) -> Task | None:
    key = task_id.strip().lower()
    for t in load_tasks(env_dir):
        if t.id.lower() == key:
            return t
    return None


def next_task(env_dir: Path, exclude: set[str] | None = None) -> Task | None:
    skip = exclude or set()
    pending = [t for t in load_tasks(env_dir) if t.needs_agent and t.id not in skip]
    if not pending:
        return None
    pending.sort(key=lambda t: (_PICK_RANK.get(t.status, 9), t.priority, t.id))
    return pending[0]


def tasks_in_review(env_dir: Path) -> list[Task]:
    return [t for t in load_tasks(env_dir) if t.in_review]


def by_status(env_dir: Path) -> dict[str, list[Task]]:
    out: dict[str, list[Task]] = {s: [] for s in LIFECYCLE}
    for t in sorted(load_tasks(env_dir), key=lambda t: (t.priority, t.id)):
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
) -> Task:
    """Create a new task JSON file and return the Task object."""
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "tasks").mkdir(exist_ok=True)
    tid = task_id or next_id(env_dir, id_prefix)
    path = env_dir / "tasks" / f"{tid}.json"
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
    )
    _save(task)
    return task


# ---------------------------------------------------------------------------
# mutations (all round-trip through _save)
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def set_status(task: Task, status: str) -> None:
    if status not in LIFECYCLE:
        raise ValueError(f"unknown status: {status!r}")
    task.status = status
    _save(task)


def submit_for_review(task: Task, agent: str, summary: str = "", tokens: str = "") -> None:
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
        task.review_log.append(ReviewEntry(
            ts=_now_iso(), event="done", summary=summary,
        ))
    set_status(task, DONE)


def log_started(task: Task, agent: str, rework: bool = False) -> None:
    task.review_log.append(ReviewEntry(
        ts=_now_iso(),
        event="rework_started" if rework else "started",
        agent=agent,
    ))
    _save(task)


def set_scratchpad(task: Task, notes: str) -> None:
    """Replace the agent's continuity note (carried to its next iteration)."""
    task.scratchpad = notes.strip()
    _save(task)


def record_decision(task: Task, decision: str, rationale: str = "",
                    agent: str | None = None) -> None:
    """Append a decision the agent made. Oracle will VERIFY it, not assume it."""
    if not decision.strip():
        return
    task.decisions.append(Decision(
        decision=decision.strip(),
        rationale=rationale.strip(),
        ts=_now_iso(),
        agent=agent,
    ))
    _save(task)


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
    """Glanceable view of every task on the track."""
    groups = by_status(env_dir)
    if not any(groups.values()):
        return "(no tasks)"
    lines: list[str] = []
    for status in LIFECYCLE:
        items = groups.get(status) or []
        if not items:
            continue
        lines.append(_STATUS_LABEL[status])
        for t in items:
            prds = f" · {', '.join(t.prds)}" if t.prds else ""
            subs = f" [{sum(1 for s in t.subtasks if s.status == DONE)}/{len(t.subtasks)} subtasks]" if t.subtasks else ""
            lines.append(f"  - [{t.id}{prds}] {t.title} (priority {t.priority}){subs}")
    return "\n".join(lines)
