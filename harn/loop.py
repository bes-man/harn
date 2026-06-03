"""The ralph-style loop, driven by harn (not by each agent's native hooks),
so behavior is identical across Claude / Codex / Cursor / Antigravity.

PLANNING -> READY -> EXECUTING -> (BLOCKED -> wait for `harn answer`) -> DONE
"""
from __future__ import annotations

from pathlib import Path

from . import skills, state, tasks
from .adapters import get_adapter
from .config import Config
from .feedback import run_feedback
from .notify import notify


def _build_prompt(env_dir: Path, task: tasks.Task, feedback_tail: str = "") -> str:
    agents_md = (env_dir.parent / "AGENTS.md")
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    skill_index = skills.index(env_dir)
    task_body = task.path.read_text(encoding="utf-8", errors="replace")

    parts = [
        base,
        "## Available skills (load only what you need)\n"
        "Read a skill's full text via the harn `read_skill` tool ONLY when the "
        "task calls for it, to keep the context window small:\n" + skill_index,
        "## Current task\n" + task_body,
        "## Rules\n"
        "- If anything is ambiguous, underspecified, risky, or you are unsure, "
        "STOP and ask: call the harn `ask_user` tool, or write your question to "
        "`harn_env/state/BLOCKED.md` and end your turn. Do NOT guess.\n"
        "- When the task is complete and tests pass, say so explicitly.",
    ]
    if feedback_tail:
        parts.append("## Last feedback (tests)\n```\n" + feedback_tail + "\n```")
    return "\n\n".join(p for p in parts if p.strip())


def run(project_root: Path, env_dir: Path, max_iterations: int | None = None) -> str:
    """Run the loop until DONE, BLOCKED, or max_iterations. Returns final phase."""
    cfg = Config.load(env_dir)
    state_dir = env_dir / "state"
    st = state.State.load(state_dir)
    adapter = get_adapter(cfg.agent)
    limit = max_iterations if max_iterations is not None else cfg.max_iterations

    if st.phase == state.BLOCKED:
        print(f"[harn] BLOCKED, waiting for an answer. Question:\n{st.question}")
        print("[harn] Provide it with: harn answer \"...\"")
        return st.phase

    feedback_tail = ""
    for _ in range(limit):
        task = tasks.next_task(env_dir)
        if task is None:
            st.transition(state.DONE)
            st.save(state_dir)
            print("[harn] No pending tasks. DONE.")
            return st.phase

        st.transition(state.EXECUTING)
        st.current_task = task.id
        st.iterations += 1
        st.save(state_dir)
        print(f"[harn] Working task '{task.id}' ({task.title}) with {adapter.name}...")

        result = adapter.run_turn(_build_prompt(env_dir, task, feedback_tail),
                                  project_root)
        print(result.text[-2000:] if result.text else "(no output)")

        # 1) Did the agent block on a question?
        question = state.read_block_question(state_dir)
        if question:
            st.block(question)
            st.save(state_dir)
            channels = notify(f"[harn] Agent needs your input on '{task.id}':\n{question}")
            print(f"[harn] BLOCKED. Notified: {channels or 'none configured'}")
            return st.phase

        # 2) Feedback signal (project test command)
        fb = run_feedback(cfg.test_cmd, project_root)
        feedback_tail = fb.tail()
        if fb.ran and not fb.ok:
            print("[harn] Tests failing; looping to let the agent fix them.")
            continue

        # 3) Task accepted
        tasks.mark_done(task, summary=f"completed via {adapter.name}")
        st.transition(state.READY)
        st.save(state_dir)
        print(f"[harn] Task '{task.id}' done.")

    print(f"[harn] Reached iteration limit ({limit}).")
    return st.phase


def answer(env_dir: Path, text: str) -> None:
    """Record a human answer, clear the block, and resume on next `harn run`."""
    state_dir = env_dir / "state"
    st = state.State.load(state_dir)
    question = st.question or "(prior question)"
    st.answer(text)
    state.clear_block_marker(state_dir)
    # Append the answer so the agent sees it next turn.
    with (state_dir / "ANSWERS.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n## Q: {question}\n{text}\n")
    st.save(state_dir)
