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

from . import design as design_mod
from . import progress as progress_mod
from . import skills as skills_mod
from . import state as state_mod
from . import tasks as tasks_mod
from .config import Config
from .feedback import run_feedback
from .notify import notify


def _env_dir() -> Path:
    return Path(os.environ.get("HARN_ENV_DIR", "harn_env")).resolve()


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
        """Save a learned fact/convention/standard into a skill — this is how harn
        BUILDS UP project knowledge so future work needs fewer questions.

        Call this whenever you learn something durable: the user answered a
        question about a standard, you discovered a convention in the codebase, or
        a decision should apply project-wide. Ask the user to confirm first (in
        the chat / via ask_user), THEN save. Creates the skill if it doesn't
        exist (e.g. `security`, `standards`, `frontend`, `testing`, `api`).

        Examples:
          save_to_skill("security", "All endpoints require auth except /health.")
          save_to_skill("frontend", "Use TanStack Query; no manual fetch in components.")
        """
        path = skills_mod.append_learning(_env_dir(), skill, content, description)
        _log(f"skill '{skill}' updated: {content[:80]}")
        return f"saved to {path.relative_to(_env_dir())}"

    @mcp.tool()
    def get_next_task() -> str:
        """Return the highest-priority task that needs agent work, or a note if
        none. Resumes an in-progress task before starting a new one."""
        t = tasks_mod.next_task(_env_dir())
        if t is None:
            return "(no pending tasks)"
        _log(f"{t.id}: picked up by agent ({t.title})")
        return t.path.read_text(encoding="utf-8")

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
    ) -> str:
        """Create (or convert) a task and write it as a JSON file in ``harn_env/tasks/``.

        Use in two ways:
        1. **From scratch** — the human described work in words; you clarified
           with ``ask_user`` first, then call this with the structured result.
        2. **From existing text** — the human pasted a description, a PRD
           section, or a Jira story; you extract and restructure it into the
           correct fields before calling this tool.

        In both cases YOU author the file; the human should not have to touch
        JSON. If anything is ambiguous, call ``ask_user`` first.

        Parameters
        ----------
        title         Short imperative title (e.g. "Add JWT auth").
        description   Markdown body with ``## What`` and ``## Done when``
                      (concrete, checkable acceptance criteria). Add a
                      ``## Notes for future agents`` section for anything the
                      next executor must know.
        prds          Parent PRD slugs (filenames without .md), e.g. ["auth"].
                      A task may span multiple PRDs — pass all of them.
        skills        harn skills the executor will need (``list_skills`` to see
                      what's available), e.g. ["security", "standards"].
        priority      Lower = sooner. Default 10.
        epic          Optional tracker Epic key, e.g. "AUTH".
        user_story    Optional parent Story/issue key, e.g. "AUTH-12".
        task_id       Leave empty → auto-generate (PRJ-NNN using ``[harn]
                      project``). Pass a tracker key (e.g. "AUTH-42") when the
                      task already exists in Jira/Linear.
        """
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
        )
        return f"Created task {t.id}: {t.path.name}"

    @mcp.tool()
    def update_task(
        task_id: str,
        description: str = "",
        skills: list[str] | None = None,
        prds: list[str] | None = None,
        priority: int | None = None,
    ) -> str:
        """Update fields on an existing task. Primary use: during the planning
        phase the agent writes refined acceptance criteria back into the task.

        Only the fields you pass are updated; omitted fields are left as-is.
        harn-managed fields (status, review_log, id) cannot be changed here.

        Parameters
        ----------
        task_id      The task id (e.g. "PRJ-001" or "AUTH-42").
        description  Full Markdown body. Must include ``## What`` and
                     ``## Done when`` with concrete, verifiable criteria.
        skills       Replace the skills list (e.g. ["security", "standards"]).
        prds         Replace the PRD list (e.g. ["auth", "payments"]).
        priority     New priority (lower = sooner).
        """
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
        """Record a decision you made while working a task, with its rationale.

        Use this whenever you make a non-obvious choice (a library, an approach, a
        trade-off, an assumption). Two reasons:
          1. **Continuity** — your next iteration sees what you already decided,
             so you don't re-derive it and you stay consistent (saves tokens).
          2. **Verification** — these are CLAIMS, not facts. The independent
             oracle review will check each decision against the acceptance
             criteria and PRD. So state the real reason, not a justification.

        Example: record_decision("AUTH-42", "Use sqlite, not postgres",
                                 "no network dependency; single-node deployment")
        """
        t = tasks_mod.find(_env_dir(), task_id)
        if t is None:
            return f"(task '{task_id}' not found)"
        tasks_mod.record_decision(t, decision, rationale)
        return f"Recorded decision on {task_id} ({len(t.decisions)} total)"

    @mcp.tool()
    def set_scratchpad(task_id: str, notes: str) -> str:
        """Save a short note to your future self for the NEXT iteration of this
        task. Replaces the previous note (keep it current, not a log).

        Put here: what's done, what's left, gotchas to remember. Keep it short —
        this is lightweight continuity, not a full transcript. It is NOT part of
        the task's acceptance criteria; the oracle treats it as your working
        state, not as a requirement.

        Example: set_scratchpad("AUTH-42", "Done: /login + middleware. "
                 "Left: /refresh rotation. Gotcha: tokens are UTC, watch tz.")
        """
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
        """Record a clarifying question in harn (BLOCKED state + Telegram
        escalation). This tool does NOT display anything to the human by
        itself — its arguments are collapsed in the chat UI.

        ⚠️ PRESENTATION ORDER — do this in the SAME turn, then STOP:

        **Claude Code**: call the native `AskUserQuestion` tool FIRST
        (renders interactive clickable option buttons). Then call this tool.

        **Cursor / other chat agents**: render a visible markdown dialog FIRST,
        then call this tool:
        ```
        ---
        ❓ **[Topic]**
        [One-line context]

        | | Option | Trade-off |
        |---|---|---|
        | **(a)** | … | … |
        | **(b)** | … | … |

        ✅ Recommendation: **(a)** — [reason]
        Reply with **(a)**, **(b)**, or your own answer.
        ---
        ```

        **Codex / headless**: skip chat presentation; this tool routes the
        question to Telegram immediately (set `channel = "telegram"` in
        harn.toml for headless use).

        In ALL cases: call this tool to persist the question (enables Telegram
        escalation + skill capture). When the human answers, call
        `answer_question` with their reply, then continue.

        Write `question` EXPANDED, not one terse line:
          1. Context — what you were doing and WHY this question came up.
          2. Options — the 2-3 concrete choices, with the trade-off of each.
          3. Recommendation — the option you'd pick and a one-line reason.
        Example: "Building the login endpoint, the PRD doesn't say how long
        access tokens live. Options: (a) 15m + refresh token — most secure, more
        work; (b) 24h — simplest, weaker; (c) match an existing service.
        I recommend (a) for security; OK to proceed?"

        **`skill`**: if the question is about a durable standard/convention
        (security, frontend, testing, architecture, …), pass the skill name. The
        answer is then AUTOMATICALLY saved into that skill, so harn never has to
        ask it again. Use this for anything that should outlive the current task."""
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
        """Record the human's answer to the last `ask_user` question.

        Call this immediately after the user answers your question IN THIS CHAT,
        before continuing with any work. This clears the BLOCKED state, saves the
        answer to ANSWERS.md, promotes it into the skill (if the question had
        `skill=`), and lets `harn watch` know escalation is no longer needed.

        Workflow:
          1. You called `ask_user(...)` and stopped.
          2. The user replied here in the chat.
          3. You call `answer_question(answer=<their reply>)`.
          4. Continue the task."""
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
        """Save the UI mockup for a task — the visual contract agreed BEFORE
        implementation. Call during the planning phase of any user-facing task.

        `html` must be a SINGLE self-contained static HTML file (inline CSS, no
        external assets, realistic sample data) showing the final interface,
        including every state the acceptance criteria mention (empty, error,
        success…). It is written to ``harn_env/design/<task_id>.html``; the
        human opens it in a browser to approve. After approval the executor
        builds to it and the verification phases (oracle + Playwright browser
        pass) check the real UI against it. Saving again overwrites — iterate
        until the human approves via `ask_user`."""
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
                        "task's review_log and relay it to the user.")
        return f"(no task '{task_id}')"

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
