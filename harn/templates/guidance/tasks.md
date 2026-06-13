---
topic: tasks
summary: Author tasks with create_task; carry context cheaply between iterations; test gate.
---

# Creating tasks & carrying context

## Creating a task
When the human describes work (or points at a PRD), YOU author the task via the
`create_task` MCP tool — don't hand-write JSON, don't make them format it.
1. **Clarify first** if goal/scope/criteria are fuzzy (expanded `ask_user`).
2. **Call `create_task`** with: `title` (imperative), `description` (Markdown
   with `## What`, `## Done when` — concrete checkable criteria, what
   verify/oracle check), `prds` (parent slug(s)), `skills` (needed harn skills),
   optional `task_id` (tracker key, else auto `PRJ-NNN`), `epic`/`user_story`,
   `depends_on` (real ordering only).
3. Keep tasks small and reviewable; split big asks into several under the PRD.

## Tests
Every code change needs coverage — aim for one test per acceptance criterion.
harn gates on this: a turn that changes code without touching tests is sent
back with a nudge. If something genuinely can't be tested, record why with
`record_decision`. After changes, run `run_tests`; never mark complete while
tests fail.

## Carry context between iterations (cheaply)
You run as a fresh process each turn — but the task file persists:
- `record_decision(task_id, decision, rationale)` for every non-obvious choice
  (a library, an approach, a trade-off, an assumption). State the REAL reason.
- `set_scratchpad(task_id, notes)` — a short memo to your future self: what's
  done, what's left, gotchas. Brief, not a transcript.
Your next iteration sees both, so you stay consistent without re-deriving. These
are working state, **not** acceptance criteria — the oracle verifies decisions
against requirements, so don't use them to justify shortcuts.
