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
    context, tagged to the currently claimed task (STATE.json's current_task).

    Skill/tool NAMES live in every prompt (cheap index), but a body only enters
    the agent's context when it explicitly calls one of these read_* tools —
    that decision happens live inside the agent's own turn, invisible to harn
    unless recorded here. This is the only way to later answer "what actually
    ended up in this task's context" (`harn trace`, the studio board's Context
    section) instead of just "what was AVAILABLE to load".
    """
    try:
        env = _env_dir()
        st = state_mod.State.load(env / "state")
        if st.current_task:
            events_mod.emit(env, "context_read", task_id=st.current_task,
                            kind=kind, name=name)
    except Exception:
        pass


def _ensure_watch_running(env_dir: Path) -> None:
    """Auto-start `harn watch` as a background daemon if not already running.

    Called once when the MCP server starts so neither the user nor the agent
    needs to remember to launch it. Uses a PID file for idempotency.
    """
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



# `start_watch=False` is for INTROSPECTION-ONLY callers (`tool_catalog()`) that
# just need the registered tools' names/docstrings — they must never have the
# side effect of spawning a real, detached `harn watch` daemon or minting a
# spurious "chat" run_id. Every REAL entry point (an actual agent session,
# `harn mcp`) keeps the default. (Deliberately a comment, not this function's
# own docstring — test_guidance.py's fixed-overhead budget sweeps every
# triple-quoted string found in build_server's source as a proxy for
# agent-facing token cost, and this note is maintainer-facing only.)
def build_server(start_watch: bool = True):
    from mcp.server.fastmcp import FastMCP, Image  # lazy: core CLI has no hard dep

    mcp = FastMCP("harn")

    if start_watch:
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

        # Low autonomy (≤ 30%): MCP itself gates on developer confirmation so
        # the block fires regardless of Auto Mode / headless hints.
        if cfg.autonomy <= 0.3:
            q = (
                f"Task {t.id} claimed: \"{t.title}\".\n\n"
                "Before I make ANY file changes, please tell me:\n"
                "1. What should I focus on or get right?\n"
                "2. Any constraints or things to avoid?\n"
                "3. Or simply confirm: \"proceed as the task describes.\"\n\n"
                "(Autonomy is ≤ 30% — every implementation decision needs "
                "explicit approval before I act.)"
            )
            state_dir = env / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            if not state_mod.read_block_question(state_dir):
                state_mod.blocked_marker(state_dir).write_text(
                    q, encoding="utf-8")
                st = state_mod.State.load(state_dir)
                st.block(q)
                st.current_task = t.id
                st.save(state_dir)
                _log(f"{t.id}: awaiting developer confirmation (autonomy ≤ 30%)")
            out += (
                "\n\n⛔ CONFIRMATION REQUIRED (autonomy ≤ 30%)\n"
                "harn has registered a clarifying question in BLOCKED state — "
                "`harn watch` will escalate to Telegram if unanswered.\n"
                "PRESENT THE QUESTION BELOW to the developer NOW via your "
                "native `AskUserQuestion` tool, then STOP.\n"
                "Do NOT make any file changes until you call "
                "`answer_question(their_answer)` to clear the block.\n\n"
                f"Question to show:\n---\n{q}\n---"
            )
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

    return mcp


_catalog_cache: dict[str, str] | None = None


def tool_catalog() -> dict[str, str]:
    """name -> full docstring for every registered MCP tool.

    The single source of truth for "what does this tool do, when do I use it" —
    reused by the studio UI's Tools tab so descriptions never drift out of sync
    with what the agent itself sees via `tools/list`. Cached (the tool set is
    static for a given harn install); build once, reuse across studio requests.
    """
    global _catalog_cache
    if _catalog_cache is None:
        import asyncio
        mcp = build_server(start_watch=False)
        tools = asyncio.run(mcp.list_tools())
        _catalog_cache = {t.name: (t.description or "").strip() for t in tools}
    return _catalog_cache


def serve(http: bool = False, host: str = "127.0.0.1", port: int = 8765) -> None:
    mcp = build_server()
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
    env = {**os.environ, "HARN_ENV_DIR": str(env_dir)}
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
