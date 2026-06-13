"""harn MCP server — the universal capability layer.

Every supported agent (Claude, Codex, Cursor, Antigravity, Qwen Code) speaks
MCP, so the same tools behave identically everywhere. Transport defaults to stdio (most
local: the agent spawns this as a subprocess); pass http=True to serve over
127.0.0.1 instead, which can later be exposed publicly behind auth/TLS.

The server acts on a single harn_env, resolved from $HARN_ENV_DIR or ./harn_env.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import codebase as codebase_mod
from . import design as design_mod
from . import guidance as guidance_mod
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


def build_server():
    from mcp.server.fastmcp import FastMCP  # imported lazily so core CLI has no hard dep

    mcp = FastMCP("harn")

    # Auto-start the watch dispatcher so Telegram escalation, oracle, and live
    # status work without the user having to run a separate command.
    _ensure_watch_running(_env_dir())

    @mcp.tool()
    def list_skills() -> str:
        """List installed skills as 'name: description'. Cheap to read; use it to
        decide which skill bodies to pull in with read_skill (saves context)."""
        return skills_mod.index(_env_dir())

    @mcp.tool()
    def read_skill(name: str) -> str:
        """Return the full body of one skill. Call ONLY when the task needs it."""
        body = skills_mod.read_skill(_env_dir(), name)
        return body if body is not None else f"(no skill named '{name}')"

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
        return body

    @mcp.tool()
    def list_services() -> str:
        """Index of registered services/modules: name + one-line responsibility
        (from harn_env/services/). Scan it to decide WHICH parts of the system a
        task touches — then `read_service` only those. This is the cheap AS-IS
        entry point; don't re-explore the repo for what's registered here."""
        return codebase_mod.index(_env_dir())

    @mcp.tool()
    def read_service(name: str) -> str:
        """Full knowledge file for one service/module: its responsibility (what
        it owns / doesn't own), the standards any change must follow, hard
        constraints, and gotchas. Call ONLY for services the current task
        touches — that's the point of the per-service split."""
        body = codebase_mod.read(_env_dir(), name)
        if body is None:
            return (f"(no service '{name}' registered) If it exists in the code, "
                    "explore it and register it via `save_service`. Template:\n\n"
                    + codebase_mod.TEMPLATE)
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
        out = t.path.read_text(encoding="utf-8")
        out += ("\n\n▶ Run the pre-task protocol from AGENTS.md before any code: "
                "AS IS → TO BE → skills (name them) → best practices → clarify.")
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
        not soft preference — use priority for that)."""
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
        )
        dep = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
        return f"Created task {t.id}: {t.path.name}{dep}"

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
            "or plain chat text), do it NOW in this same turn. Then STOP — resume "
            "only after the user answers and you call `answer_question`."
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
                tasks_mod.submit_for_review(t, agent="agent", summary=summary)
                _log(f"{task_id}: submitted for review — oracle will check it")
                return (f"task '{task_id}' submitted for review. The oracle (run "
                        "by `harn watch`) will verify it; read its verdict in the "
                        "task's review_log and relay it to the user.\n\n"
                        "➡️ NEXT: call `reconcile_skills(\"" + task_id + "\")` to "
                        "capture what this task taught into skills before moving on.")
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

    return mcp


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
