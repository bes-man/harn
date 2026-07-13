"""harn MCP server — the universal capability layer.

Every supported agent (Claude, Codex, Cursor, Antigravity, Qwen Code) speaks
MCP, so the same tools behave identically everywhere. Transport defaults to stdio (most
local: the agent spawns this as a subprocess); pass http=True to serve over
127.0.0.1 instead, which can later be exposed publicly behind auth/TLS.

The server acts on a single harn_env, resolved from $HARN_ENV_DIR or ./harn_env.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import attachments as attachments_mod
from . import codebase as codebase_mod
from . import design as design_mod
from . import events as events_mod
from . import guidance as guidance_mod
from . import prd as prd_mod
from . import progress as progress_mod
from . import skills as skills_mod
from . import state as state_mod
from . import tasks as tasks_mod
from . import tools as tools_mod
from .config import Config
from .feedback import run_feedback
from .notify import notify


def _env_dir() -> Path:
    return Path(os.environ.get("HARN_ENV_DIR", "harn_env")).resolve()


def _default_worker() -> str:
    """Identify this MCP server instance as a worker for task claiming. Each
    agent connection spawns its own server subprocess, so the PID is a stable
    per-worker id for the life of that agent's session. Override with
    HARN_WORKER for deterministic multi-process runs."""
    return os.environ.get("HARN_WORKER", "").strip() or f"pid-{os.getpid()}"


def _log(msg: str) -> None:
    """Append to the shared PROGRESS feed so `harn watch` and any agent can see
    what's happening, regardless of where the work runs (chat or `harn run`)."""
    try:
        progress_mod.log(_env_dir(), msg, agent="agent")
    except Exception:
        pass


def _context_read(kind: str, name: str) -> None:
    """Record that a skill/service/PRD/guidance body was actually pulled into
    context, tagged to the currently claimed task (STATE.json's current_task)
    and, for a sequential step, the currently running step.

    Skill/tool NAMES live in every prompt (cheap index), but a body only enters
    the agent's context when it explicitly calls one of these read_* tools —
    that decision happens live inside the agent's own turn, invisible to harn
    unless recorded here. This is the only way to later answer "what actually
    ended up in this task's context" (`harn trace`, the studio board's Context
    section) instead of just "what was AVAILABLE to load" — and, via `step_id`,
    what a Phase 4 post-step usage audit (`loop._audit_step_usage`) needs to
    tell "used" from "unused" for a step's required/recommended skills.

    step_id resolution mirrors `_record_tool_used` below: `HARN_STEP_ID` (set
    for a parallel-wave member's worktree-local MCP config) takes priority,
    falling back to `State.current_step` for a sequential step.
    """
    try:
        env = _env_dir()
        st = state_mod.State.load(env / "state")
        if st.current_task:
            step_id = os.environ.get("HARN_STEP_ID", "") or st.current_step or ""
            events_mod.emit(env, "context_read", task_id=st.current_task,
                            step_id=step_id or None, kind=kind, name=name)
    except Exception:
        pass


def _ensure_watch_running(env_dir: Path) -> None:
    """Auto-start `harn watch` as a background daemon if not already running.

    Called once when the MCP server starts so neither the user nor the agent
    needs to remember to launch it. Uses a PID file for idempotency.

    Never runs under the test suite (`PYTEST_CURRENT_TEST`, set automatically
    by pytest for the duration of every test): a test calling `build_server()`
    directly — without mocking `subprocess.Popen` — would otherwise spawn a
    REAL, fully-detached (`start_new_session=True`) daemon that outlives the
    test's tmp_path forever, since nothing tears it down. This bit us for
    real: dozens of these accumulated across a long session and one was still
    polling a live project with the default "claude" agent, spending real API
    cost via an unattended oracle turn. Tests that deliberately exercise this
    function's own logic (`tests/test_watch_autostart.py`) mock
    `subprocess.Popen` anyway and explicitly clear this env var first.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    state_dir = env_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / "watch.pid"

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)   # raises if dead
            return             # already running
        except (ProcessLookupError, ValueError, OSError):
            pid_file.unlink(missing_ok=True)

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "harn", "watch", str(env_dir.parent)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
    except Exception:
        pass  # watch is optional — never block the MCP server



def _make_tool_function(tool, project_root: Path, record_used=None):
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

    `record_used`, when supplied, is `build_server`'s `_record_tool_used`
    helper — the SAME one the `mcp.tool` usage-tracking wrapper calls.
    Custom tools register via `mcp.add_tool`, which bypasses that wrapper,
    so we emit the `tool_used` event here instead. Without this a custom
    tool would never appear in `harn trace`/metrics and would always render
    as "unused" in a step's Tools line even when the agent called it.
    """
    arg_sig = ", ".join(f"{p}: str = ''" for p in tool.params)
    call_kwargs = ", ".join(f"'{p}': {p}" for p in tool.params)
    src = (
        f"def _custom_tool({arg_sig}) -> str:\n"
        f"    return _run({{{call_kwargs}}})\n"
    )

    def _run(args):
        if record_used is not None:
            record_used(tool.name)
        return tools_mod.execute(tool, args, project_root)

    ns: dict = {"_run": _run}
    exec(src, ns)  # noqa: S102 — src is built entirely from validated [a-z0-9_]+ names
    fn = ns["_custom_tool"]
    fn.__name__ = tool.name
    fn.__doc__ = tool.description or f"Custom tool: {tool.name}"
    return fn


# `start_watch=False` is for INTROSPECTION-ONLY callers (`tool_catalog()`) that
# just need the registered tools' names/docstrings — they must never have the
# side effect of spawning a real, detached `harn watch` daemon or minting a
# spurious "chat" run_id. Every REAL entry point (an actual agent session,
# `harn mcp`) keeps the default. (Deliberately a comment, not this function's
# own docstring — test_guidance.py's fixed-overhead budget sweeps every
# triple-quoted string found in build_server's source as a proxy for
# agent-facing token cost, and this note is maintainer-facing only.)
def _scoped_tool_allowlist() -> set[str] | None:
    """The set of tool names this MCP session may register, or None for
    unrestricted (every registered tool available, today's only behavior).

    Every tool -- all ~35 built-ins plus every custom tool -- is otherwise
    visible to EVERY step's agent turn regardless of what that step's own
    `tools`/`tools_recommended` declare (those were prompt hints only, never
    enforced). A real incident: a parallel-wave step called its SIBLING's
    tool, then used project-wide navigation tools (`get_next_task`, `board`)
    to wander off into an entirely unrelated task mid-turn.

    A step opts into this by setting `tool_mode: "scoped"` in its own plan
    (Studio's per-step "Tool mode" control) -- restricting is NOT safe to
    force by default, since some steps genuinely need the full catalog.
    Resolved from HARN_TASK_ID/HARN_STEP_ID (set by `_replicate_connectors`
    for a parallel-wave worktree, or by a single-step delegated run) against
    that task's OWN saved plan. Four effective states fall out of just
    `tool_mode` + the two existing lists:
      - unset/"auto" (or no step context at all)      -> None (unrestricted)
      - "scoped", tools=[], tools_recommended=[]        -> empty set (no tools)
      - "scoped", tools_recommended=[...]               -> those, optional
      - "scoped", tools=[...]                           -> those, required
    """
    task_id = os.environ.get("HARN_TASK_ID", "")
    step_id = os.environ.get("HARN_STEP_ID", "")
    if not task_id or not step_id:
        return None
    try:
        from . import workflows as workflows_mod
        plan = workflows_mod.load_task_plan(_env_dir(), task_id)
    except Exception:
        return None
    if not plan:
        return None
    step = next((n for n in plan.get("nodes", [])
                if n.get("kind") == "step" and n.get("id") == step_id), None)
    if step is None or (step.get("tool_mode") or "auto") != "scoped":
        return None
    return ({t for t in (step.get("tools") or []) if t} |
            {t for t in (step.get("tools_recommended") or []) if t})


def build_server(start_watch: bool = True, register_custom: bool = True):
    from mcp.server.fastmcp import FastMCP, Image  # lazy: core CLI has no hard dep

    mcp = FastMCP("harn")
    _scoped_allowed = _scoped_tool_allowlist()

    # Tag every MCP tool call with the currently claimed task (and, for a
    # sequential step, the currently running step) so studio can later show
    # which skills/tools a step actually used. Wraps mcp.tool() ONCE here
    # instead of touching each of the 35+ individual tool functions below.
    # Step-id resolution: HARN_STEP_ID (set by _replicate_connectors for a
    # parallel-wave member's worktree-local MCP config, since one wave has N
    # concurrently-active steps that State.current_step can't represent)
    # takes priority, falling back to State.current_step (a sequential
    # step, Task 4). Deliberately a comment, not a docstring — see the
    # test_guidance.py note above `build_server` about its fixed triple-
    # quote token-budget sweep.
    def _record_tool_used(tool_name: str) -> None:
        try:
            env = _env_dir()
            st = state_mod.State.load(env / "state")
            task_id = os.environ.get("HARN_TASK_ID", "") or st.current_task or ""
            if not task_id:
                return
            step_id = os.environ.get("HARN_STEP_ID", "") or st.current_step or ""
            events_mod.emit(env, "tool_used", task_id=task_id,
                            step_id=step_id or None, tool=tool_name,
                            run_id=os.environ.get("HARN_RUN_ID") or None)
        except Exception:
            pass

    _orig_tool = mcp.tool

    def _tracked_tool(*deco_args, **deco_kwargs):
        inner_decorator = _orig_tool(*deco_args, **deco_kwargs)

        def wrap(fn):
            if _scoped_allowed is not None and fn.__name__ not in _scoped_allowed:
                return fn  # this step is scoped and didn't declare this tool
            import functools

            @functools.wraps(fn)
            def traced(*args, **kwargs):
                _record_tool_used(fn.__name__)
                return fn(*args, **kwargs)
            return inner_decorator(traced)
        return wrap

    mcp.tool = _tracked_tool
    # Stash so a hot-reload tick (called from outside build_server, even from
    # the watcher thread) can pass the same tool-usage tracker into freshly
    # registered custom tools that the boot loop below uses.
    mcp._harn_record_tool_used = _record_tool_used

    # A workflow worker inherits its parent's correlation id. It must not
    # create a competing chat run in the shared event stream.
    if start_watch and not os.environ.get("HARN_RUN_ID"):
        # Auto-start the watch dispatcher so Telegram escalation, oracle, and
        # live status work without the user having to run a separate command.
        _ensure_watch_running(_env_dir())

        # Open a correlation scope for this chat session so every logged cycle
        # (submit, reconcile, oracle) joins up under one run_id in events.jsonl.
        events_mod.new_run(_env_dir(), kind="chat")

    @mcp.tool()
    def list_skills() -> str:
        """List installed skills as 'name: description'. Cheap to read; use it to
        decide which skill bodies to pull in with read_skill (saves context)."""
        return skills_mod.index(_env_dir())

    @mcp.tool()
    def read_skill(name: str = "", skill: str = "", skill_name: str = "") -> str:
        """Return the full body of one skill. Call ONLY when the task needs it.
        Pass the skill name as `name` (aliases `skill`/`skill_name` also work)."""
        n = (name or skill or skill_name).strip()
        if not n:
            return 'provide the skill name, e.g. read_skill(name="ui")'
        body = skills_mod.read_skill(_env_dir(), n)
        if body is not None:
            _context_read("skill", n)
        return body if body is not None else f"(no skill named '{n}')"

    @mcp.tool()
    def save_to_skill(skill: str, content: str, description: str = "") -> str:
        """Append a durable fact/convention/standard to a skill (creates it if
        absent, e.g. security/standards/frontend/testing/api). Call when you
        learn something project-wide — a user answer, a discovered convention.
        Confirm with the user first. So future work reads it instead of asking."""
        path = skills_mod.append_learning(_env_dir(), skill, content, description)
        _log(f"skill '{skill}' updated: {content[:80]}")
        return f"saved to {path.relative_to(_env_dir())}"

    @mcp.tool()
    def read_guidance(topic: str) -> str:
        """Read one on-demand guidance topic (harn_env/guidance/<topic>.md) —
        situational protocol detail kept out of the always-on AGENTS.md. Pull a
        topic only when the task is in it. Topics: services, code-search, hil,
        parallel, design, browser, onboarding, tasks. Call with no match to see
        the index."""
        env = _env_dir()
        body = guidance_mod.read(env, topic)
        if body is None:
            return (f"(no guidance topic '{topic}'). Available:\n"
                    + guidance_mod.index(env))
        _context_read("guidance", topic)
        return body

    @mcp.tool()
    def list_services() -> str:
        """Index of registered services/modules: name + one-line responsibility
        (from harn_env/services/). Scan it to decide WHICH parts of the system a
        task touches — then `read_service` only those. This is the cheap AS-IS
        entry point; don't re-explore the repo for what's registered here."""
        return codebase_mod.index(_env_dir())

    @mcp.tool()
    def read_service(name: str = "", service: str = "") -> str:
        """Full knowledge file for one service/module: its responsibility (what
        it owns / doesn't own), the standards any change must follow, hard
        constraints, and gotchas. Call ONLY for services the current task
        touches. Pass the service name as `name` (alias `service`)."""
        n = (name or service).strip()
        if not n:
            return 'provide the service name, e.g. read_service(name="auth-api")'
        body = codebase_mod.read(_env_dir(), n)
        if body is None:
            return (f"(no service '{n}' registered) If it exists in the code, "
                    "explore it and register it via `save_service`. Template:\n\n"
                    + codebase_mod.TEMPLATE)
        _context_read("service", n)
        return body

    @mcp.tool()
    def save_service(name: str, responsibility: str, content: str) -> str:
        """Register/refresh a service file in harn_env/services/ (full replace —
        read_service first, carry over what's true).
        `name` — slug (e.g. "auth-api"). `responsibility` — ONE line (what it
        owns / doesn't own; shown in the index so agents judge relevance).
        `content` — markdown: ## Responsibility / ## Standards / ## Constraints /
        ## Gotchas. Rules and limits any change must respect, NOT a code walkthrough."""
        p = codebase_mod.save(_env_dir(), name, responsibility, content)
        _log(f"service '{name}' registered/updated ({len(content)} chars)")
        return f"saved {p.name} — the index now lists '{name}'."

    @mcp.tool()
    def ensure_skill(domain: str) -> str:
        """Install an industry-baseline skill for a domain that has none, and
        return its body. Call when a task touches a domain with no covering
        skill. Domains: frontend, backend, api, testing, security,
        accessibility, performance, database (else nothing installed — use
        save_to_skill). Then capture project deviations via save_to_skill."""
        from . import skill_library
        md = skill_library.install(_env_dir(), domain)
        if md is None:
            avail = ", ".join(skill_library.LIBRARY.keys())
            return (f"(no library baseline for '{domain}'). Available: {avail}. "
                    f"For a custom domain, use `save_to_skill('{domain}', …)`.")
        _log(f"bootstrapped baseline skill '{domain}' from library")
        body = skills_mod.read_skill(_env_dir(), domain) or ""
        return (f"Installed baseline skill '{domain}'. Now refine it with this "
                f"project's specifics via save_to_skill/ask_user.\n\n{body}")

    @mcp.tool()
    def get_next_task(worker: str = "") -> str:
        """Claim and return the highest-priority runnable task, or a note if
        none. Dependency-aware (skips tasks whose `depends_on` aren't all done)
        and atomic per `worker` id (two parallel agents never get the same one;
        you resume your own). Appends the service-registry index and a skill-gap
        note (DATA the agent needs); the pre-task protocol itself is in
        AGENTS.md — run it, don't expect it re-pasted here."""
        from . import skill_library
        env = _env_dir()
        wid = worker.strip() or _default_worker()
        t = tasks_mod.next_task(env, claim=True, worker=wid)
        if t is None:
            return "(no runnable tasks — all done, blocked by deps, or claimed)"
        _log(f"{t.id}: claimed by {wid} ({t.title})")
        # Tag STATE.json's current_task unconditionally (not just the low-
        # autonomy block below) — this is what `_context_read` tags read_skill/
        # read_service/read_prd/read_guidance calls against, so "what actually
        # ended up in this task's context" is traceable in chat mode too, not
        # only in `harn run`'s own loop (which sets it per turn separately).
        state_dir = env / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        st = state_mod.State.load(state_dir)
        st.current_task = t.id
        st.save(state_dir)
        # Render this task's workflow into WORKFLOW.md so read_workflow (and
        # file-reading agents) see the right flow. Empty → the project default.
        from . import workflows as wfs
        active_wf = wfs.activate(env, t.workflow)
        out = t.path.read_text(encoding="utf-8")
        if t.workflow:
            out += f"\n\n▶ Workflow: '{active_wf}' — re-read WORKFLOW.md now."
        out += ("\n\n▶ Run the pre-task protocol from AGENTS.md before any code: "
                "AS IS → TO BE → skills (name them) → best practices → clarify.")
        # Surface the configured autonomy level so harn autonomy actually
        # governs ask-vs-decide in chat mode (it only reached headless before).
        from .config import autonomy_directive
        cfg = Config.load(env)
        out += "\n\n" + autonomy_directive(cfg.autonomy)
        out += "\n\n" + codebase_mod.prompt_note(env)

        note = skill_library.gap_note(env, t)
        if note:
            out += "\n\n" + note
        # Surface available parallelism so the agent can offer to fan out.
        others = [r for r in tasks_mod.runnable_tasks(env) if r.id != t.id]
        if others:
            ids = ", ".join(r.id for r in others)
            out += (f"\n\n## ⚡ Parallelism available\n{len(others)} other "
                    f"task(s) are runnable right now with no dependency on this "
                    f"one: {ids}. They can be worked in PARALLEL. Tell the user "
                    f"they can speed things up by running multiple agents (extra "
                    f"chat windows, or — if your runtime can spawn subagents — "
                    f"offer to fan them out, one task per worker id).")
        return out

    @mcp.tool()
    def runnable_tasks() -> str:
        """List every task that could START RIGHT NOW (deps satisfied, not
        claimed) WITHOUT claiming any. The count is the available parallelism:
        if it returns 2+ tasks, they're independent and can run concurrently
        across multiple agents/windows. Use this to decide whether to suggest
        parallelizing — then each worker claims one via `get_next_task(worker)`."""
        rs = tasks_mod.runnable_tasks(_env_dir())
        if not rs:
            return "(nothing runnable right now — all done, blocked, or claimed)"
        lines = [f"{len(rs)} task(s) runnable in parallel:"]
        for t in rs:
            dep = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
            lines.append(f"  - {t.id}: {t.title} (priority {t.priority}){dep}")
        if len(rs) > 1:
            lines.append("→ These are independent; they can run concurrently. "
                         "Offer the user parallel execution.")
        return "\n".join(lines)

    @mcp.tool()
    def create_task(
        title: str,
        description: str,
        prds: list[str],
        skills: list[str],
        priority: int = 10,
        epic: str = "",
        user_story: str = "",
        task_id: str = "",
        depends_on: list[str] | None = None,
        workflow: str = "",
    ) -> str:
        """Author a task as JSON in harn_env/tasks/ (YOU write it, not the
        human; clarify with ask_user first if fuzzy).
        title — imperative ("Add JWT auth"). description — Markdown with
        `## What` + `## Done when` (concrete, checkable criteria verify/oracle
        check) + optional notes. prds — parent slug(s) e.g. ["auth"]. skills —
        skills the executor needs. priority — lower = sooner (default 10).
        epic / user_story — optional tracker keys. task_id — empty → auto
        PRJ-NNN, else a tracker key. depends_on — ids that must be `done` first;
        leave empty so independent tasks run in PARALLEL (real ordering only,
        not soft preference — use priority for that). workflow — named preset
        (read_workflow footer); empty → default."""
        from .config import Config
        cfg = Config.load(_env_dir())
        t = tasks_mod.create_task(
            _env_dir(),
            title=title,
            description=description,
            prds=prds,
            priority=priority,
            skills=skills,
            epic=epic or None,
            user_story=user_story or None,
            task_id=task_id or None,
            id_prefix=cfg.project.upper(),
            depends_on=depends_on or None,
            workflow=workflow or None,
        )
        if workflow:
            # An agent explicitly passing a named workflow is itself the
            # "explicit action to set task.workflow" the in_progress gate
            # wants (see studio.set_task_workflow) — a human opening this
            # task later shouldn't hit the "pick a flow" refusal for a flow
            # that was already explicitly chosen at creation time.
            t.workflow_confirmed = True
            tasks_mod._save(t)
        dep = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
        wf = f" [workflow: {t.workflow}]" if t.workflow else ""
        return f"Created task {t.id}: {t.path.name}{dep}{wf}"

    @mcp.tool()
    def update_task(
        task_id: str,
        description: str = "",
        skills: list[str] | None = None,
        prds: list[str] | None = None,
        priority: int | None = None,
    ) -> str:
        """Update an existing task (only the fields you pass; status/review_log/
        id are harn-managed). Main use: write refined `## What` + `## Done when`
        criteria back during planning. skills/prds replace the list; priority
        lower = sooner."""
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        changed: list[str] = []
        if description:
            t.description = description
            changed.append("description")
        if skills is not None:
            t.skills = list(skills)
            changed.append("skills")
        if prds is not None:
            t.prds = list(prds)
            changed.append("prds")
        if priority is not None:
            t.priority = int(priority)
            changed.append("priority")
        if changed:
            tasks_mod._save(t)
        return f"Updated {task_id}: {', '.join(changed) or 'nothing changed'}"

    @mcp.tool()
    def read_prd(slug: str) -> str:
        """Read a PRD's full text on demand (harn_env/prd/<slug>.md). Needed for
        the WHY/scope during PLANNING; after `lock_spec` the task carries the
        distilled criteria, so you rarely re-read the PRD while implementing —
        which is why it isn't injected every turn."""
        p = prd_mod.find(_env_dir(), slug)
        if p is None:
            return f"(no PRD '{slug}'). Create harn_env/prd/{slug}.md"
        _context_read("prd", slug)
        return p.raw.strip()

    @mcp.tool()
    def lock_spec(task_id: str, done_when: str, approach: str = "",
                  decisions: list[str] | None = None) -> str:
        """CLOSE the clarification funnel: write the narrowed, verified spec into
        the task and lock it. Call at the END of planning, once your clarifying
        questions have eliminated the ambiguity — not before.

        `done_when` — the authoritative acceptance criteria, one observable,
        independently verifiable fact per line (this becomes `## Done when`).
        `approach` — optional: the chosen implementation direction, with the
        rejected alternatives DROPPED (the funnel's whole point).
        `decisions` — optional "decision :: rationale" strings recording what
        was settled with the human.

        After this, executor turns trust the locked spec and the full PRD is
        read-on-demand (`read_prd`) — saving tokens with no loss of fidelity,
        because the spec already distilled it."""
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        decs = []
        for d in (decisions or []):
            dec, _, why = d.partition("::")
            decs.append((dec, why))
        tasks_mod.lock_spec(t, done_when, approach, decs)
        _log(f"{task_id}: spec locked ({len(done_when)} chars criteria)")
        return (f"Spec locked for {task_id}. The funnel is closed — implement "
                "exactly the locked `## Done when`; re-open only if the human "
                "changes requirements.")

    @mcp.tool()
    def record_decision(task_id: str, decision: str, rationale: str = "") -> str:
        """Record a non-obvious choice (library, approach, trade-off, assumption)
        + its real rationale. Carries to your next iteration (no re-deriving) and
        is VERIFIED by the oracle against the criteria — state the real reason,
        not a justification for a shortcut."""
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        tasks_mod.record_decision(t, decision, rationale)
        return f"Recorded decision on {task_id} ({len(t.decisions)} total)"

    @mcp.tool()
    def record_change(task_id: str, summary: str, detail: str = "") -> str:
        """Log a SIGNIFICANT change, release-notes style — call after finishing a
        meaningful piece of work. ONE concise line per change, NOT a per-edit
        diary. `summary` = what shipped (user-facing voice); `detail` = decisions/
        standards it set. Never injected into prompts (zero context cost); feeds
        `generate_changelog`. No-op when [log] changes = false."""
        cfg = Config.load(_env_dir())
        if not cfg.log_changes:
            return "(change logging off — [log] changes = false)"
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        tasks_mod.record_change(t, summary, detail)
        _log(f"{task_id}: change logged — {summary[:100]}")
        return f"Change logged on {task_id} ({len(t.changelog)} total)"

    @mcp.tool()
    def generate_changelog(task_id: str = "", write: bool = False) -> str:
        """Assemble docs (release notes) from tasks' changelog + decisions,
        timestamped, one section per task. `task_id` scopes to one task;
        `write=true` saves to harn_env/CHANGELOG.md. Use when the human asks for
        a changelog/release notes/summary of what was built."""
        env = _env_dir()
        md = tasks_mod.render_changelog(env, task_id or None)
        if write:
            out = env / "CHANGELOG.md"
            out.write_text(md, encoding="utf-8")
            _log(f"changelog written to {out.name}")
            return f"Written to {out}.\n\n{md}"
        return md

    @mcp.tool()
    def set_scratchpad(task_id: str, notes: str) -> str:
        """Save a short note to your next iteration (replaces the previous one):
        what's done, what's left, gotchas. Lightweight continuity, not a log —
        NOT acceptance criteria (oracle treats it as working state)."""
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        tasks_mod.set_scratchpad(t, notes)
        return f"Scratchpad saved on {task_id}"

    @mcp.tool()
    def board() -> str:
        """Show every task and where it sits on the track
        (todo → in_progress → review → changes_requested → done). Use it to see
        what's done and what's planned before you start."""
        return tasks_mod.board(_env_dir())

    @mcp.tool()
    def ask_user(question: str, skill: str = "") -> str:
        """Persist a clarifying question (BLOCKED state + Telegram escalation +
        skill capture). This tool shows nothing by itself — its args are
        collapsed. FIRST surface the question in your client's native
        interactive UI (AskUserQuestion / Plan Mode), THEN call this, same turn,
        then STOP. See read_guidance("hil") for per-client detail.

        Write `question` EXPANDED: context + why, 2-3 options with trade-offs,
        your recommendation. `skill` — if about a durable standard, pass the
        skill name so the answer is saved into it automatically (never re-asked).
        When the human answers, call `answer_question`, then continue."""
        state_dir = _env_dir() / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_mod.blocked_marker(state_dir).write_text(question, encoding="utf-8")
        state_mod.set_block_skill(state_dir, skill)
        st = state_mod.State.load(state_dir)
        st.block(question)
        st.save(state_dir)
        _log(f"asked the user: {question.splitlines()[0][:120]}"
             + (f" [→ skill: {skill}]" if skill else ""))
        # `harn watch` (or the run loop) turns this into an interactive Telegram
        # card with escalation — we don't send a one-way push here.
        return (
            "Question recorded (BLOCKED). `harn watch` will route it to the user "
            "(waits chat_grace_minutes, then escalates to Telegram). If you have "
            "NOT yet shown the question to the human (native AskUserQuestion tool "
            "or plain chat text), do it NOW in this same turn. Then STOP.\n\n"
            "**Resuming:** when the human sends any message — even 'ok' or 'continue' "
            "— call `check_pending_answer()` FIRST. If the answer came via Telegram "
            "while you were stopped, it returns the text. If 'still_waiting', show "
            "the question again via AskUserQuestion. If the human's message IS the "
            "answer, call `answer_question(answer)` as usual."
        )

    @mcp.tool()
    def answer_question(answer: str) -> str:
        """Record the human's answer to the last `ask_user` question (call right
        after they reply in chat). Clears BLOCKED, saves to ANSWERS.md, promotes
        into the skill if the question had `skill=`, and cancels Telegram
        escalation. Then continue the task."""
        from . import loop as loop_mod
        env_dir = _env_dir()
        state_dir = env_dir / "state"
        if not state_mod.read_block_question(state_dir):
            return "No pending question found — nothing to answer."
        loop_mod.answer(env_dir, answer)
        _log(f"answer recorded in chat: {answer[:120]}")
        return "Answer recorded. Block cleared. You may continue the task."

    @mcp.tool()
    def check_pending_answer() -> str:
        """Check if a pending ask_user question was answered via Telegram or the
        'Decide for me' button while you were stopped. Call on resume when the
        human's message is ambiguous ('ok', 'continue') rather than the answer.
        Returns the answer text if found, 'still_waiting', or
        'no_pending_answer'."""
        env_dir = _env_dir()
        state_dir = env_dir / "state"
        pending_file = state_dir / "PENDING_TELEGRAM_ANSWER.txt"
        if pending_file.exists():
            ans = pending_file.read_text(encoding="utf-8").strip()
            pending_file.unlink(missing_ok=True)
            _log(f"chat agent consumed Telegram answer: {ans[:80]}")
            return f"Answer received (via Telegram/auto): {ans}"
        q = state_mod.read_block_question(state_dir)
        if q:
            return f"still_waiting — question still pending: {q[:200]}"
        return "no_pending_answer"

    @mcp.tool()
    def save_design(task_id: str, html: str) -> str:
        """Save the UI mockup (the pre-implementation visual contract) for a
        user-facing task → harn_env/design/<task_id>.html. `html` = a SINGLE
        self-contained static file (inline CSS, sample data) showing the final
        interface incl. empty/error/success states. Human approves it via
        ask_user before code; overwrites on re-save. See read_guidance("design")."""
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        p = design_mod.save(_env_dir(), task_id, html)
        _log(f"{task_id}: design mockup saved ({len(html)} chars)")
        return (f"Design saved to {p}. Ask the human to open it in a browser "
                "and confirm via `ask_user` before implementation starts.")

    @mcp.tool()
    def read_design(task_id: str) -> str:
        """Return the approved UI mockup (HTML) for a task, or a note if none
        exists. Read it before implementing or verifying user-facing work."""
        html = design_mod.load(_env_dir(), task_id)
        return html if html is not None else f"(no design for '{task_id}')"

    @mcp.tool()
    def save_attachment(task_id: str, filename: str, content_b64: str) -> str:
        """Save a file (base64-encoded) to a task's attachments, e.g. a
        reference image the human shared or one you generated. Never
        overwrites — a name collision auto-suffixes. See read_attachment."""
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception:
            return "content_b64 is not valid base64"
        p = attachments_mod.save(_env_dir(), task_id, filename, data)
        _log(f"{task_id}: attachment saved ({p.name}, {len(data)} bytes)")
        return f"Saved as {p.name} ({len(data)} bytes). list_attachments('{task_id}') to see all."

    @mcp.tool()
    def list_attachments(task_id: str) -> str:
        """List a task's saved files (name, size, image/file) — check before
        generating or re-saving one with the same purpose."""
        rows = attachments_mod.list_files(_env_dir(), task_id)
        if not rows:
            return f"(no attachments for '{task_id}')"
        return "\n".join(f"- {r['name']} ({r['kind']}, {r['size']}B)" for r in rows)

    @mcp.tool()
    def read_attachment(task_id: str, filename: str):
        """Return one attachment. Images come back as real image content (you
        SEE it, e.g. a design reference) — other files as raw text."""
        p = attachments_mod.path_for(_env_dir(), task_id, filename)
        if p is None:
            return f"(no attachment '{filename}' on '{task_id}')"
        if attachments_mod.is_image(p.name):
            return Image(path=str(p))
        try:
            return p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return f"({p.name} is binary/unreadable as text, {p.stat().st_size}B)"

    @mcp.tool()
    def run_tests() -> str:
        """Run the project's configured feedback command and return the tail."""
        cfg = Config.load(_env_dir())
        fb = run_feedback(cfg.test_cmd, _env_dir().parent)
        return f"ran={fb.ran} ok={fb.ok}\n{fb.tail()}"

    @mcp.tool()
    def submit_for_review(task_id: str, summary: str = "") -> str:
        """Signal that a task is finished and ready for HUMAN review (call after
        tests pass). This moves it to `review` — it does NOT mark it done; only
        the human accepts. harn also submits automatically at the end of your
        turn, so calling this is an explicit, optional signal with a summary."""
        for t in tasks_mod.load_tasks(_env_dir()):
            if t.id == task_id:
                # Deterministic documentation backstop: guarantee a changelog
                # entry exists even if the agent never called record_change.
                from . import loop as loop_mod
                loop_mod._auto_changelog(_env_dir(), Config.load(_env_dir()), t,
                                         summary)
                t = tasks_mod.find(_env_dir(), task_id) or t
                tasks_mod.submit_for_review(t, agent="agent", summary=summary)
                _log(f"{task_id}: submitted for review — oracle + reconcile run next")
                events_mod.emit(_env_dir(), "cycle_end", task_id=task_id,
                                outcome="submitted", summary=summary[:200] or None)
                return (
                    f"task '{task_id}' submitted for review.\n\n"
                    "Do these steps IN ORDER — do not skip either:\n\n"
                    "1. **Reconcile**: call `reconcile_skills(\"" + task_id + "\")` "
                    "NOW. Read the brief it returns, then call `save_to_skill` for "
                    "each confident convention ([auto] prefix) and "
                    "`ask_user(skill=…)` for any trade-off needing developer input. "
                    "End by saying `RECONCILE: DONE`.\n\n"
                    "2. **Oracle**: `harn watch` runs the oracle in the background. "
                    "Call `board()` to check the verdict. Relay the oracle's "
                    "PASS / FAIL / DEBT result to the developer. If FAIL, the task "
                    "returns to `changes_requested` and you rework it next. "
                    "If the oracle hasn't run yet, note it and move on — it will "
                    "appear in the task's review_log."
                )
        return f"(no task '{task_id}')"

    @mcp.tool()
    def reconcile_skills(task_id: str = "") -> str:
        """Capture what a finished task taught (call after submit_for_review).
        Returns a brief — changed files + existing skills + instructions to
        auto-save confident conventions via save_to_skill ([auto] prefix), ask
        the human about trade-offs via ask_user(skill=…), and refresh touched
        service files. This is how harn learns from its own work."""
        from . import skill_library
        env = _env_dir()
        t = tasks_mod.find(env, task_id) if task_id else None
        if t is None:
            # Fall back to the most recently active task.
            st = state_mod.State.load(env / "state")
            t = tasks_mod.find(env, st.current_task) if st.current_task else None
        if t is None:
            return ("(no task to reconcile — pass a task_id). After finishing "
                    "work, call this so harn learns from it.")
        return skill_library.reconcile_brief(env, env.parent, t)

    @mcp.tool()
    def loop_status() -> str:
        """Current harn LOOP phase, active task, and pending question (this is
        the ralph-loop state, not an OS/service healthcheck)."""
        st = state_mod.State.load(_env_dir() / "state")
        return f"phase={st.phase} task={st.current_task} question={st.question}"

    @mcp.tool()
    def explain_pipeline() -> str:
        """Show the active workflow's steps, in order, with each step's
        agent/model (or the run default). Tells the user what will happen
        before work starts."""
        from . import loop as loop_mod
        env_dir = _env_dir()
        return loop_mod.explain(env_dir, Config.load(env_dir))

    @mcp.tool()
    def read_workflow() -> str:
        """Return harn_env/WORKFLOW.md — the always-followed flow + required skills
        per step. Read at session start; load each step's required + relevant
        skills (never all). Footer lists named presets a task can run under."""
        from . import workflow as wf
        from . import workflows as wfs
        env = _env_dir()
        p = env / wf.FILENAME
        body = p.read_text(encoding="utf-8") if p.exists() else "(no WORKFLOW.md yet)"
        rows = wfs.list_workflows(env)
        if len(rows) > 1:   # only worth showing when presets beyond 'default' exist
            active = wfs.active_name(env)
            names = ", ".join((m["name"] + ("*" if m["name"] == active else ""))
                              for m in rows)
            body += (f"\n\n<!-- workflow presets (set_task_workflow): {names} "
                     f"(*=active) -->")
        return body

    @mcp.tool()
    def save_workflow(content: str) -> str:
        """Write harn_env/WORKFLOW.md. Use in onboarding to save the workflow you
        co-authored with the user. Keep the shape `## N. Step` + `Skills
        (required: …)` so harn can parse mandatory skills."""
        from . import workflow as wf
        p = _env_dir() / wf.FILENAME
        p.write_text(content, encoding="utf-8")
        _log("workflow saved")
        return f"WORKFLOW.md saved ({len(content)} chars)."

    @mcp.tool()
    def set_task_workflow(task_id: str, workflow: str) -> str:
        """Run a task under a named workflow preset; empty resets to the default.
        Preset names are in read_workflow's footer / the studio UI."""
        from . import workflows as wfs
        env = _env_dir()
        t = tasks_mod.find(env, task_id)
        if t is None:
            return f"(no task {task_id})"
        slug = (workflow or "").strip().lower()
        known = [m["name"] for m in wfs.list_workflows(env)]
        if slug and wfs._slug(slug) not in known:
            return (f"(no workflow '{workflow}' — have: {', '.join(known)}; "
                    f"create new ones in the studio UI)")
        t.workflow = wfs._slug(slug) if slug else None
        tasks_mod._save(t)
        _log(f"{t.id} workflow → {t.workflow or 'default'}")
        return f"{t.id} will run under workflow: {t.workflow or 'default (WORKFLOW.md)'}"

    # `register_custom=False` is for INTROSPECTION-ONLY callers (`tool_catalog()`)
    # that need JUST the static set of built-in tools — never the dynamically
    # discovered custom tools. Skipping the loop keeps `tool_catalog()`'s
    # module-level cache genuinely valid (the built-in set really IS static)
    # and stops a custom tool from being double-listed in the studio Tools tab
    # or polluting the name-uniqueness gate with a stale cached entry. Real
    # agent sessions keep the default (register_custom=True).
    # Snapshot of built-in tool names, captured BEFORE any custom tool is
    # registered below (only the ~35 built-ins added via @mcp.tool() above
    # exist in the ToolManager at this point). This is the allow/deny line
    # the hot-reload reconciler uses to refuse to ever add, remove, or
    # replace a built-in — see `_reconcile_custom_tools` and the Finding-1
    # writeup in .superpowers/sdd/task-3-report.md.
    builtin_names = {t.name for t in mcp._tool_manager.list_tools()}
    mcp._harn_builtin_names = builtin_names

    if register_custom:
        for custom_tool in tools_mod.discover(_env_dir()):
            # Defense-in-depth: `tools.save()` validates a tool before it ever
            # reaches disk, but `discover()` reads harn_env/tools/*.json
            # directly with no validation of its own — a hand-edited file, a
            # future Import feature, or a tool bundle shared by another user
            # could land a name colliding with a built-in, a non-list
            # `params`, or an unsafe param name here. `_custom_tool_is_registerable`
            # is the single shared predicate (also used by `_reconcile_custom_tools`
            # and `start_tool_reload`'s seed) so this boot loop can never drift
            # out of sync with the reconciler again.
            if not _custom_tool_is_registerable(custom_tool, builtin_names):
                _log(f"custom tool '{custom_tool.name}' skipped at boot: "
                     f"fails safety predicate (built-in name, non-list "
                     f"params, or unsafe param name)")
                continue
            if _scoped_allowed is not None and custom_tool.name not in _scoped_allowed:
                continue  # this step is scoped and didn't declare this tool
            fn = _make_tool_function(custom_tool, _env_dir().parent,
                                     _record_tool_used)
            mcp.add_tool(fn, name=custom_tool.name,
                         description=custom_tool.description)

        try:
            interval = Config.load(_env_dir()).mcp_tool_reload_seconds
        except Exception:
            interval = 2
        start_tool_reload(mcp, interval, builtin_names)

    return mcp


def _custom_tool_is_registerable(ct, builtin_names: set) -> bool:
    """True iff this discovered custom tool is safe to register: its name is
    not a built-in, `params` is a list, and every param is a safe string.

    The single source of truth for "may this discovered tool touch the live
    ToolManager" — used by the `build_server` boot loop, `_reconcile_custom_tools`
    (building `safe`), AND `start_tool_reload`'s `_loop` seed. Before this
    helper existed the three sites re-implemented the same checks by hand and
    drifted: the boot loop had no params-is-a-list guard (a malformed
    `"params": 123` on disk crashed `build_server()` at every startup) and the
    daemon's seed had no `builtin_names` filter at all (a planted
    `read_skill.json` could enter the seed `registered` set, so the very next
    reconcile tick saw it in `registered` but not in `safe` and deleted the
    real `read_skill` built-in via the remove-path). Never raises — `discover()`
    reads untrusted json off disk with no schema validation of its own."""
    if ct.name in builtin_names:
        return False
    if not isinstance(ct.params, list):
        return False
    return all(tools_mod.is_safe_param_name(p) for p in ct.params)


def _tools_dir_signature(env_dir: Path) -> tuple:
    """Cheap change-detector for harn_env/tools/: (name, mtime, size) per json,
    sorted. Never raises — a stat error yields an empty signature (treated as
    'no tools'), which the reconciler handles safely."""
    d = env_dir / "tools"
    try:
        out = []
        for p in sorted(d.glob("*.json")):
            st = p.stat()
            out.append((p.name, st.st_mtime, st.st_size))
        return tuple(out)
    except Exception:
        return ()


def _reconcile_custom_tools(mcp, env_dir: Path, registered: set,
                            builtin_names: set | None = None) -> set:
    """Make the live ToolManager match the SAFE custom tools on disk. Adds
    new/changed, removes deleted, skips unsafe-param tools (same gate as the
    boot loop above). Returns the new registered-name set. Best-effort, never
    raises.

    `builtin_names` (the server's built-in tool names, captured by
    `build_server()` BEFORE any custom tool is registered) is the hard
    boundary this function must never cross: a discovered custom tool whose
    `name` collides with a built-in is skipped outright, before it can ever
    enter `safe` — so it can never enter `registered` either, which means
    the remove-path below (keyed off `registered`) can never call
    `remove_tool`/`add_tool` on a built-in name. Without this, a planted
    `harn_env/tools/read_skill.json` could delete-and-replace the real
    `read_skill` built-in on the next reconcile tick (trust hijack + DoS)."""
    builtin_names = builtin_names or set()
    tm = mcp._tool_manager
    try:
        discovered = {t.name: t for t in tools_mod.discover(env_dir)}
    except Exception as exc:
        _log(f"tool reload: discover failed ({exc}); keeping current set")
        return registered
    safe = {}
    for name, ct in discovered.items():
        # Single shared predicate — also used by the `build_server` boot loop
        # and `start_tool_reload`'s seed — so the three sites can't drift out
        # of sync (that drift is exactly what let a planted read_skill.json
        # delete the real built-in; see the helper's docstring).
        if not _custom_tool_is_registerable(ct, builtin_names):
            _log(f"tool reload: '{name}' skipped: fails safety predicate "
                 f"(built-in name, non-list params, or unsafe param name)")
            continue
        safe[name] = ct
    changed = False
    # Remove tools that vanished or became unsafe. `name not in builtin_names`
    # is defense-in-depth: `safe` above already excludes built-in names, so
    # `registered` (built from `safe` on a prior tick) should never contain
    # one — but this guarantees `remove_tool` can never touch a built-in even
    # if `registered` were somehow poisoned (e.g. a caller seeding it by hand).
    for name in list(registered):
        if name not in safe and name not in builtin_names:
            try:
                tm.remove_tool(name)
            except Exception:
                pass
            changed = True
    # Add/replace current safe tools (always re-add so an edited command/params
    # takes effect — remove-then-add makes it idempotent).
    record_used = getattr(mcp, "_harn_record_tool_used", None)
    new_reg = set()
    for name, ct in safe.items():
        try:
            if name in {t.name for t in tm.list_tools()}:
                tm.remove_tool(name)
            fn = _make_tool_function(ct, env_dir.parent, record_used)
            mcp.add_tool(fn, name=ct.name, description=ct.description)
            new_reg.add(name)
            changed = True
        except Exception as exc:
            _log(f"tool reload: '{name}' failed to register ({exc})")
    if changed:
        _notify_tools_changed(mcp)
    return new_reg


def _notify_tools_changed(mcp) -> None:
    """Best-effort: tell connected clients the tool list changed. If no active
    session is reachable from this thread, the ToolManager is still correct so
    the next tools/list is fresh anyway. Never raises."""
    try:
        session = mcp._mcp_server.request_context.session
        import anyio
        anyio.from_thread.run(session.send_tool_list_changed)
    except Exception:
        pass


def _seed_registered(mcp, env_dir: Path, builtin_names: set | None = None) -> set:
    """Compute `start_tool_reload`'s initial `registered` set: the names the
    reconcile loop is allowed to consider "ours" on its very first tick.

    A discovered custom tool only counts if it passes `_custom_tool_is_registerable`
    — in particular, a name that collides with a built-in (e.g. an attacker or
    a hand-edited file planting `harn_env/tools/read_skill.json` BEFORE the
    daemon starts) must never enter this set. Without that filter the built-in
    would be seeded into `registered`, then the first reconcile tick would see
    it correctly absent from `safe` (the reconciler excludes built-ins) and
    delete it via the remove-path — destroying a real built-in tool and never
    re-adding it. Factored out as a standalone module-level function (rather
    than an inline closure in `_loop`) so it's independently unit-testable.
    Never raises — falls back to an empty set on any `discover()` failure."""
    builtin_names = builtin_names or set()
    try:
        discovered = {c.name: c for c in tools_mod.discover(env_dir)}
        live_names = {t.name for t in mcp._tool_manager.list_tools()}
        return {
            name for name, ct in discovered.items()
            if name in live_names
            and _custom_tool_is_registerable(ct, builtin_names)
        }
    except Exception:
        return set()


def start_tool_reload(mcp, interval_s: int, builtin_names: set | None = None) -> None:
    """Daemon watcher: poll harn_env/tools/ every interval_s and reconcile the
    live server. interval_s <= 0 disables it. Never blocks server startup and
    never lets an unexpected exception escape the loop (a request thread must
    never see this thread crash).

    `builtin_names` is threaded straight into every `_reconcile_custom_tools`
    call so the watcher can never touch a built-in tool name (see that
    function's docstring)."""
    if interval_s <= 0:
        return
    env_dir = _env_dir()
    builtin_names = builtin_names or set()

    def _loop():
        registered = _seed_registered(mcp, env_dir, builtin_names)
        last = _tools_dir_signature(env_dir)
        while True:
            try:
                time.sleep(interval_s)
                sig = _tools_dir_signature(env_dir)
                if sig != last:
                    # Only advance `last` AFTER the reconcile call returns —
                    # a tick that changes nothing (or raises inside
                    # _reconcile_custom_tools, though it's best-effort and
                    # shouldn't) must not silently skip a legitimate
                    # co-located change by advancing the signature early.
                    registered = _reconcile_custom_tools(mcp, env_dir,
                                                          registered,
                                                          builtin_names)
                    last = sig
            except Exception:
                # Never let the daemon die or take the process down with it.
                continue

    threading.Thread(target=_loop, name="harn-tool-reload", daemon=True).start()


_catalog_cache: dict[str, str] | None = None


def tool_catalog() -> dict[str, str]:
    """name -> full docstring for every registered MCP tool.

    The single source of truth for "what does this tool do, when do I use it" —
    reused by the studio UI's Tools tab so descriptions never drift out of sync
    with what the agent itself sees via `tools/list`. Built with
    `register_custom=False` so this is JUST the ~35 built-ins (the dynamically
    discovered custom tools are listed separately by the studio). That set IS
    static for a given harn install, which is what makes the module-level cache
    below valid — build once, reuse across studio requests.
    """
    global _catalog_cache
    if _catalog_cache is None:
        import asyncio
        mcp = build_server(start_watch=False, register_custom=False)
        tools = asyncio.run(mcp.list_tools())
        _catalog_cache = {t.name: (t.description or "").strip() for t in tools}
    return _catalog_cache


def serve(http: bool = False, host: str = "127.0.0.1", port: int = 8765) -> None:
    # A PROBE invocation (healthcheck()'s spawned subprocess, e.g. the studio's
    # MCP health badge polling every 1.5s) sets HARN_MCP_PROBE so it never
    # mints a "chat" run_id or starts the watch daemon — it only wants a
    # quick, side-effect-free tools/list answer. Without this, a frequent
    # poller floods events.jsonl with bogus run_start events that clobber the
    # shared "current run" pointer (events.new_run's .run_id file), starving
    # progress_payload()'s active-stage detection for the REAL running loop —
    # the exact cause of the Flow tab getting stuck showing "starting…"
    # forever while a real run is actively executing under a different id.
    probe = os.environ.get("HARN_MCP_PROBE") == "1"
    managed_worker = bool(os.environ.get("HARN_RUN_ID"))
    mcp = build_server(start_watch=not probe and not managed_worker)
    if http:
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio


def healthcheck(env_dir, timeout: int = 40) -> tuple[bool, list[str], str]:
    """Launch the stdio MCP server in a subprocess and confirm it answers.

    Returns (ok, tool_names, error). Used by `harn setup` / `harn doctor` to
    verify the server actually starts and exposes its tools, before the user
    discovers it's broken from inside an agent.
    """
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "harn-doctor", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "\n".join(json.dumps(m) for m in msgs) + "\n"
    # HARN_MCP_PROBE tells serve() this is a health probe, not a real agent
    # session — see the comment on serve() for why that distinction matters.
    env = {**os.environ, "HARN_ENV_DIR": str(env_dir), "HARN_MCP_PROBE": "1"}
    try:
        p = subprocess.run([sys.executable, "-m", "harn", "mcp"],
                           input=stdin, capture_output=True, text=True,
                           timeout=timeout, env=env)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout) or ""
    except Exception as e:
        return False, [], f"could not launch `harn mcp`: {e}"

    tools: list[str] = []
    for line in out.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("id") == 2 and "result" in m:
            tools = [t["name"] for t in m["result"].get("tools", [])]
    if not tools:
        return False, [], "MCP server started but returned no tools"
    return True, tools, ""
