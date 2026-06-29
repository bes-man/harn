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
calling it.

**Resuming after ask_user (bidirectional answers):**

Answers can arrive from two channels — chat or Telegram. When the human sends
any message after you've called `ask_user`:

1. **Their message IS the answer** (clear response matching the question) →
   call `answer_question(answer)` to record it, then continue.
2. **Their message is ambiguous** ('ok', 'continue', 'proceed') → call
   `check_pending_answer()` FIRST.
   - `"Answer received (via Telegram/auto): …"` → the answer came from Telegram
     while you were stopped. Use it; if `skill=` was set, call `save_to_skill`.
   - `"still_waiting"` → the block is still active; show the question again via
     AskUserQuestion and wait.
   - `"no_pending_answer"` → block was cleared elsewhere; proceed normally.

`harn watch` starts automatically when the MCP server connects — do NOT ask the
user to run it. It handles Telegram escalation, oracle, live status, and idle
detection in the background. Headless (`harn run`): if unsure, `ask_user` (or
write `harn_env/state/BLOCKED.md`) and harn relays it to the human.

**Idle notification:** if the agent is silent (no PROGRESS updates) for longer
than `chat_grace_minutes` (default 5 min), `harn watch` sends a Telegram nudge
to the developer. This is informational — not an error. No action needed unless
the task has actually stalled.
