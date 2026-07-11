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
    # Release-notes-style log of significant changes (see ChangeEntry). Governed
    # by [log] changes; append-only; doc-source, NOT injected into work prompts.
    changelog:   list[ChangeEntry] = field(default_factory=list)
    # Git commit captured when work first started — `harn rollback` returns the
    # working tree to this point to redo the task from scratch.
    baseline_ref: str = ""
    review_log:  list[ReviewEntry] = field(default_factory=list)
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
    # Durable per-step results ledger ({step_id: {...}}) — the step-execution
    # engine's record of what happened for each step in the task's workflow
    # snapshot (harn_env/tasks/<id>.workflow.json). Keyed by step id so it
    # survives step re-ordering/renaming in the plan.
    step_results: dict = field(default_factory=dict)

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
        step_results=dict(d.get("step_results") or {}),
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
        "step_results": task.step_results,
    }


def to_dict(task: Task) -> dict:
    """The full task as a JSON-safe dict (status, workflow, scratchpad, decisions,
    review_log, …) — how the studio UI's board renders task detail without
    re-deriving the shape `_to_dict` already owns."""
    return _to_dict(task)


def _save(task: Task) -> None:
    task.path.write_text(
        json.dumps(_to_dict(task), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
    return [_load(p) for p in sorted(tasks_dir.glob("*.json"))
            if not p.name.endswith(".workflow.json")]


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
    depends_on: list[str] | None = None,
    workflow: str | None = None,
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
        depends_on=list(depends_on or []),
        workflow=(workflow or None),
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
        fresh = _load(task.path)
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
        fresh = _load(task.path)
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
        fresh = _load(task.path)
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
        by_id = {x.id: x for x in load_tasks(env_dir)}
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
