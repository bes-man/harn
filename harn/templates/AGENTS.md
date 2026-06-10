# AGENTS.md

Portable instructions for any agent (Claude Code, Codex, Cursor, Antigravity,
Qwen Code) working in this repository through **harn**.

## Two ways harn runs
- **Headless** (`harn run`): harn launches you non-interactively for one focused
  turn, runs tests, verifies, and submits your work for review. No human is
  watching your chat, so when you're unsure you call `ask_user` (or write
  `harn_env/state/BLOCKED.md`) and harn relays it to the human (Telegram/CLI).
- **Interactive (in a chat with me)**: you are the *hands*; `harn watch` is the
  *dispatcher* running in a terminal. Do NOT shell out to `harn run` (that nests
  a second agent). Instead BE the loop yourself, and follow this protocol:

  1. **Report every step in the chat.** Before each action say what you're doing
     ("Picking up AUTH-42…", "Running tests…", "Submitting for review…"). The
     human must always be able to see what harn is doing from the chat.
  2. `get_next_task` → read only the skills you need → implement → `run_tests`.
  3. **Don't stall.** After `submit_for_review`, immediately `get_next_task` and
     start the next one — do NOT wait for the review to come back. A task in
     `review` is the dispatcher's job, not yours.
  4. **Oracle runs out-of-band.** After you submit, `harn watch` runs an
     independent oracle on the task. **Poll the task's `review_log`** (re-read it
     via `get_next_task`/`board`) for an `oracle_pass` / `oracle_fail` /
     `oracle_debt` entry and **relay the verdict to me in the chat**. If
     `oracle_fail`, the task is back in `changes_requested` — rework it.
  5. **Questions:** ask me directly in the chat. If I might be away, also call
     `ask_user` — the dispatcher posts it to Telegram with escalation and an
     answer in either place resolves it. Never guess.

  ⚠️ For Telegram, oracle, and escalation to work in chat mode, **`harn watch`
  must be running** in a terminal. If it isn't, tell me to start it.

## Code search (search before reading)

harn integrates two optional code-search backends. Use whichever is available
in your tool list — harn's prompts will tell you which tools to call.

**SocratiCode** (static dependency graph — more precise for impact analysis):
- `codebase_impact("symbol")` — what breaks if this symbol changes (blast radius)
- `codebase_symbol("symbol")` — definition + all callers + all callees
- `codebase_search("query")` — hybrid semantic+BM25 search over the whole repo

**semble** (semantic chunk retrieval — lightweight, no Docker needed). Tools are
named `search` / `find_related` (your client may prefix them, e.g.
`mcp__semble__search`):
- `search("topic")` → only the relevant chunks (~98% fewer tokens than reading
  files). Call this before opening any file.
- `find_related("file.py", 42)` → semantic neighbours of a changed line.

**Protocol** (regardless of which backend is available):
1. Search first — never open a whole file when a search can narrow it down.
2. SocratiCode > semble for "what depends on X" questions (static is precise).
3. semble > grepping manually for "find code similar to X" questions.
4. Read full files only when you need context outside the returned chunks.

**Language note:** semantic search (semble's default model) is tuned for English
code identifiers. Even if the task/PRD is in another language, phrase code
searches with the actual **code symbols** (English identifiers), not natural-
language task wording — e.g. search `verifyToken`, not «проверка токена».
SocratiCode's `codebase_impact`/`codebase_symbol` and `find_related` work on the
dependency graph and are language-independent.

## How to work here
- This project is driven by harn. Pick up work from `harn_env/tasks/`, follow the
  active PRD in `harn_env/prd/`, and respect the standards in `harn_env/skills/`.
- **Skills are loaded on demand.** Do not read every skill. Use the harn
  `list_skills` tool to see what exists, then `read_skill` only the ones the
  current task needs. This keeps the context window small.
- **Plan before you build — as a dialog, not a wall of text.** First write the
  acceptance criteria into the task (`update_task`). Then resolve open questions
  **one at a time**: ask a single focused question (context + options +
  recommendation), wait for the answer, capture it (into the task or a skill),
  then ask the next. Never dump a list of questions in one message. Only start
  coding once the criteria are confirmed.
- **When unsure, stop and ask.** If anything is ambiguous, risky, or
  underspecified, call the harn `ask_user` tool (or write your question to
  `harn_env/state/BLOCKED.md` and end your turn). Never guess on ambiguous work.
  **Ask expanded, not terse:** state (1) the context and *why* the question came
  up, (2) the concrete options with each one's trade-off, and (3) your
  recommended option with a one-line reason — so the human can decide quickly.
  **If the question is about a durable standard/convention, pass the `skill=`
  argument** (e.g. `ask_user(question, skill="security")`) — harn then saves the
  answer into that skill AUTOMATICALLY, so it's never asked again.
- **Build up the knowledge base.** harn gets smarter as it learns the project.
  Whenever you learn something durable — the user answers a question about a
  standard, you discover a convention in the code, or a decision should apply
  project-wide — **confirm it with the user, then call `save_to_skill(skill,
  content)`** (e.g. `security`, `standards`, `frontend`, `testing`, `api`). It
  creates the skill if missing. Next time, you read it instead of asking again.
  An answer you don't capture is a question you'll ask twice.
- **Design before code (user-facing tasks).** If a task changes anything the
  user will SEE, generate a single-file static HTML mockup of the final
  interface first and save it with `save_design(task_id, html)` — it lands in
  `harn_env/design/<task_id>.html`. Ask the human to open it and confirm
  (`ask_user`), iterating until approved. The approved mockup is the visual
  contract: build to it, and tag the task with the `ui` skill (`update_task`)
  so the browser verification phase runs on it. `read_design(task_id)` returns
  it later.
- **Feedback loop.** After changes, run the project's tests via the harn
  `run_tests` tool. Do not mark a task complete while tests fail.
- **Write tests for what you build.** Every code change needs test coverage —
  aim for one test per acceptance criterion. harn gates on this: a turn that
  changes code without touching tests is sent back with a nudge. If something
  genuinely can't be tested, record why with `record_decision`.
- **Verify UI work in a real browser.** When the Playwright MCP tools
  (`browser_navigate`, `browser_snapshot`, `browser_click`,
  `browser_take_screenshot`, …) are available and the task is user-facing,
  drive the running app like a user would: walk each acceptance criterion,
  compare against the approved design, and save screenshots to
  `harn_env/state/screenshots/<task_id>/`. In headless runs harn starts/stops
  the app itself (`[browser]` in `harn_env/harn.toml`) and runs this as its own
  phase; in chat mode, do it yourself before `submit_for_review`.
- **Carry context between iterations (cheaply).** You run as a fresh process each
  turn, so you don't remember the last one — but the task file does. As you work:
  - `record_decision(task_id, decision, rationale)` for every non-obvious choice
    (a library, an approach, a trade-off, an assumption). State the REAL reason.
  - `set_scratchpad(task_id, notes)` to leave your future self a short note: what's
    done, what's left, gotchas. Keep it brief — it's a memo, not a transcript.
  Your next iteration sees both, so you stay consistent without re-deriving and
  without re-reading everything (saves tokens). These are your working state —
  **not** acceptance criteria. The independent oracle review will VERIFY your
  decisions against the requirements, so don't use them to justify shortcuts.
- Make small, reviewable changes. Explain what you changed and why.

## Onboarding a new / under-specified project
harn is useless until it knows the project. If `harn_env/` is sparse — empty
`prd/`, skills are still stubs, no `[feedback] test_cmd` — **onboard first**, and
do it as a calm one-question-at-a-time dialog (never a questionnaire dump):

0. **Read `harn_env/state/ONBOARD.md`** if present (`harn onboard` writes it):
   the auto-detected stack and a brief. Also read the repo's README/docs and use
   code search (`search` / `codebase_search`) to map the code. The user may point
   you at md files with project info — read those instead of asking from scratch.
1. **What are we building?** Capture it into a PRD (`harn_env/prd/<slug>.md`:
   Problem / Goal / Scope / Acceptance criteria). Confirm with the user.
2. **What standards apply?** Ask about the ones that shape decisions — security,
   testing, frontend conventions, API style, code standards. Ask each as
   `ask_user(question, skill="<that skill>")` so the answer is saved into the
   skill AUTOMATICALLY (or call `save_to_skill` when you discover a convention in
   the code). These drive every later decision.
3. **How do we verify?** Get the test command → set `[feedback] test_cmd`.
Don't start building until the PRD + key skills are filled and confirmed. A few
minutes here means the agent decides correctly for the whole project after.

## Creating a task
When the human describes work in words (or points at a PRD like `auth`), YOU
author the task — don't make them format it. **Use the `create_task` MCP tool**;
don't hand-write JSON.
1. **Clarify first.** If the goal, scope, or acceptance criteria are fuzzy, ask
   (expanded `ask_user`) before creating it. A vague task is a bad task.
2. **Call `create_task`** with:
   - `title` — clear imperative ("Add JWT auth").
   - `description` — Markdown with `## What`, `## Done when` (concrete, checkable
     acceptance criteria — this is what verify/oracle check), and notes.
   - `prds` — the parent PRD slug(s), e.g. `["auth"]`. A task may span several.
   - `skills` — harn skills the executor will need, e.g. `["security"]`.
   - `task_id` — leave empty to auto-number (`PRJ-001`…); pass a tracker key
     (e.g. `AUTH-42`) when it already exists in Jira/Linear.
   - optional `epic` / `user_story` for tracker lineage.
   This writes `harn_env/tasks/<id>.json`. harn manages `status` and the
   `review_log`; you don't set those.
3. Keep it small and reviewable; split big asks into several tasks under the PRD.

## Project-specific notes
<!-- Fill in: domain, key commands, anything an agent must always know. -->
