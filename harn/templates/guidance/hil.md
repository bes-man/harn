---
topic: hil
summary: How to ask the human — native interactive UI per client, ask_user persistence, Telegram.
---

# Human-in-the-loop: how to ask

**Never put a choice to the human as trailing chat prose** — clarifying
questions, "start PRJ-030 now or commit first?", "approach A or B?", "ready to
proceed?". ANY question with options goes through the native interactive UI.

Per client:
- **Claude Code** — the native `AskUserQuestion` tool (clickable buttons).
  Include 2–4 options + your recommendation.
- **Cursor** — Cursor's interactive question / Plan Mode (selectable options;
  user shortcut `Shift+Tab`).
- **Codex** — Plan Mode clarifying-question flow (`/plan` or `Shift+Tab`).
- **No interactive UI** — a visible markdown block (options + recommendation)
  so the human sees it without expanding tool args.

Then, for ambiguous requirements or durable standards, ALSO call
`ask_user(question, skill=…)` to persist the question (skill capture + Telegram
escalation). **Ask expanded:** (1) context + why it came up, (2) options with
each trade-off, (3) your recommended option + one-line reason. STOP after
calling it. When the human answers, call `answer_question(answer=…)` to record
it into the skill, then continue.

`harn watch` starts automatically when the MCP server connects — do NOT ask the
user to run it. It handles Telegram escalation, oracle, and live status in the
background. Headless (`harn run`): if unsure, `ask_user` (or write
`harn_env/state/BLOCKED.md`) and harn relays it to the human.
