# spec-writer agent — design

## Problem

harn's default `WORKFLOW.md` already has a "Pre-task protocol" step (AS IS →
TO BE → best practices → clarify via `ask_user` → `lock_spec`), but it's one
step inside the main pipeline, always run inline by whichever role picks up
the task next. There's no standalone, showcase-quality agent that
demonstrates harn's role system end to end (custom role + custom workflow
preset + PRD linkage + Telegram HIL + `lock_spec`) doing the one job every
project needs before code starts: turning a vague task into a spec with real
acceptance criteria, informed by more than a quick guess.

## Goal

Ship a default role, `spec-writer`, that every new harn project gets out of
the box (via `scaffold.setup`/`harn onboard`), runnable on demand
(`/spec` in Telegram, `POST /api/agents/run`, or `harn run --as spec-writer`),
that researches a task, asks the human only what it can't resolve itself
(routed through the existing Telegram HIL), and ends by writing a
software-spec-shaped `Description` into the task and calling `lock_spec`.

## Scope

- New role: `harn/templates/agents/spec-writer.md`
- New workflow preset: `harn/templates/workflows/spec-writer.json`
- Both ship via the existing template-copy mechanism in `scaffold.setup` —
  no changes to `scaffold.py`, `roles.py`, `workflows.py`, or the MCP server
  are needed; this is pure content authored against existing mechanisms.
- Out of scope: any new MCP tool, any new HIL channel, any change to PRD
  file format, any UI change in Studio.

## Requirements

### Role frontmatter (`harn_env/agents/spec-writer.md`)

```yaml
name: spec-writer
command: spec
status: todo
trigger: manual
next_status: ""
workflow: spec-writer
oracle: false
isolation: main
push: false
```

`status: todo` matches the status `create_task` already defaults to, so
`/spec <free text>` (new task) and `/spec <task_id>` (existing backlog task)
both work without a custom board status. `next_status: ""` leaves the task in
`todo` — enriched and spec-locked, ready for the normal implementer role to
pick up via `get_next_task`. `oracle: false` because there's no status
transition to gate. `trigger: manual` — this role must never auto-fire; it's
opt-in per task.

### Workflow preset (`harn_env/workflows/spec-writer.json`) — 7 steps

Each step is a `kind: step` node (`title`/`body`/`id`/`required` skills/
`tools`), same shape the Studio canvas edits and `workflows.py` already
validates. Step bodies are prose instructions, matching the style of the
default `WORKFLOW.md`. Findings from each step land in the task's
`## Context` automatically (harn's existing append-on-every-turn behavior),
so later steps see earlier findings for free — no extra plumbing.

1. **Research** — read the task, any linked PRD (`read_prd`), and relevant
   code/skills (`project`, `architecture`). Restate the problem as AS-IS →
   TO-BE in one paragraph.
2. **Competitor analysis** — how similar products/features solve this
   problem. Web research (WebSearch / context7 where the CLI adapter
   supports it). Output: a private findings list, not a task section.
3. **Best practices** — current technical best practices and patterns for
   this kind of change (skills: `standards`, `architecture`; context7 for
   library/API specifics). Same: feeds later steps, not its own section.
4. **Risk analysis** — technical, security, and edge-case risks (skills:
   `security`, `constraints`). Each risk paired with a mitigation or an open
   question.
5. **Questions to user** — for every remaining ambiguity, `ask_user(question,
   skill=...)`, highest-leverage first, one at a time. Answers arrive via
   chat or Telegram through the existing HIL loop (`telegram.py`,
   `check_pending_answer`) — no new integration.
6. **Draft spec** — `update_task(task_id, description=...)` written in the
   structure below. If the task has `prds` set, also update
   `harn_env/prd/<slug>.md` (direct file edit, following `prd.py`'s existing
   required sections) so the global requirement isn't stranded in one task.
7. **Lock spec** — `lock_spec(task_id, done_when=..., approach=...,
   decisions=[...])`, closing the clarification funnel exactly like the
   default pipeline's step 2 does today.

### Description structure (task's `## Description`, written in step 6)

Software-spec shaped, not research-stage shaped — competitor/best-practice
findings are inputs, not headings:

```
## Problem
## Goal
## Scope
## Requirements
## Constraints & risks
## Acceptance criteria
```

`Acceptance criteria` is the same content passed to `lock_spec`'s
`done_when` (one observable, independently verifiable fact per line) —
written to the task twice is acceptable here since `lock_spec` is the
authoritative copy; the Description copy keeps the full spec human-readable
in one place.

## Constraints & risks

- **Risk: web research unavailable.** Not every CLI adapter/model
  combination has live web search. Steps 2–3 must degrade gracefully —
  the step body says "if web search isn't available, reason from training
  knowledge and flag the gap as an open question" rather than failing the
  step.
- **Risk: over-long research stalls simple tasks.** This role is opt-in
  (`trigger: manual`, invoked via `/spec`) precisely so trivial tasks skip
  it and go straight through the default pipeline's lighter step 2.
- **Constraint: no new MCP tools.** Everything here (`ask_user`, `update_task`,
  `read_prd`, `lock_spec`) already exists; this task is pure role/workflow
  content plus template wiring, keeping with harn's lean/stdlib-first
  direction.

## Acceptance criteria

- `harn/templates/agents/spec-writer.md` exists, parses via `roles.discover`
  with `status="todo"`, `trigger="manual"`, `next_status=""`,
  `workflow="spec-writer"`, `oracle=False`, `isolation="main"`.
- `harn/templates/workflows/spec-writer.json` exists, loads via
  `workflows.load`/`_norm` without error, and contains exactly 7 step nodes
  in the order above, each with a non-empty `title` and `body`.
- Scaffolding a fresh project (`scaffold.setup`) copies both files into
  `harn_env/agents/spec-writer.md` and `harn_env/workflows/spec-writer.json`,
  and `roles.discover(env)` finds the role there.
- `__version__` in `harn/__init__.py` and the version in `pyproject.toml` are
  bumped as part of this change (project convention: every committed
  `harn/` change bumps the version).
