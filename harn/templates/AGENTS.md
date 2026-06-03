# AGENTS.md

Portable instructions for any agent (Claude Code, Codex, Cursor, Antigravity)
working in this repository through **harn**.

## How to work here
- This project is driven by harn. Pick up work from `harn_env/tasks/`, follow the
  active PRD in `harn_env/prd/`, and respect the standards in `harn_env/skills/`.
- **Skills are loaded on demand.** Do not read every skill. Use the harn
  `list_skills` tool to see what exists, then `read_skill` only the ones the
  current task needs. This keeps the context window small.
- **Plan before you build.** In planning, restate the task, list assumptions, and
  surface open questions. Only start coding once the plan is confirmed.
- **When unsure, stop and ask.** If anything is ambiguous, risky, or
  underspecified, call the harn `ask_user` tool (or write your question to
  `harn_env/state/BLOCKED.md` and end your turn). Never guess on ambiguous work.
- **Feedback loop.** After changes, run the project's tests via the harn
  `run_tests` tool. Do not mark a task complete while tests fail.
- Make small, reviewable changes. Explain what you changed and why.

## Project-specific notes
<!-- Fill in: domain, key commands, anything an agent must always know. -->
