# AGENTS.md

Portable instructions for any agent (Claude Code, Codex, Cursor, Antigravity,
Qwen Code) working in this repository through **harn**.

## Two ways harn runs
- **Headless** (`harn run`): harn launches you non-interactively for one focused
  turn, runs tests, verifies, and submits your work for review. No human is
  watching your chat, so when you're unsure you call `ask_user` (or write
  `harn_env/state/BLOCKED.md`) and harn relays it to the human (Telegram/CLI).
- **Interactive (in a chat with me)**: you are driving, and I'm right here. Do
  NOT shell out to `harn run` (that would nest a second agent). Instead BE the
  loop yourself: call `get_next_task`, read only the skills you need, implement
  the change, call `run_tests`, then self-verify against the task's acceptance
  criteria. **Ask me your questions directly in this chat** — I answer inline.
  When I approve, I'll accept the task (`harn review … --approve`) or tell you to
  move on. Use `board` anytime to show me the track.

  If I might step away, call `ask_user` anyway: the question waits in the chat
  first, then escalates to Telegram after the configured grace
  (`[notify] chat_grace_minutes`), and an answer in either place resolves it — as
  long as a coordinator is alive (`harn watch`). Either way, never guess.

## Code search (search before reading)

harn integrates two optional code-search backends. Use whichever is available
in your tool list — harn's prompts will tell you which tools to call.

**SocratiCode** (static dependency graph — more precise for impact analysis):
- `codebase_impact("symbol")` — what breaks if this symbol changes (blast radius)
- `codebase_symbol("symbol")` — definition + all callers + all callees
- `codebase_search("query")` — hybrid semantic+BM25 search over the whole repo

**semble** (semantic chunk retrieval — lightweight, no Docker needed):
- `semble_search("topic")` → only the relevant chunks (~98% fewer tokens than
  reading files). Call this before opening any file.
- `semble_find_related("file.py", 42)` → semantic neighbours of a changed line.

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
- **Plan before you build.** In planning, restate the task, list assumptions, and
  surface open questions. Only start coding once the plan is confirmed.
- **When unsure, stop and ask.** If anything is ambiguous, risky, or
  underspecified, call the harn `ask_user` tool (or write your question to
  `harn_env/state/BLOCKED.md` and end your turn). Never guess on ambiguous work.
  **Ask expanded, not terse:** state (1) the context and *why* the question came
  up, (2) the concrete options with each one's trade-off, and (3) your
  recommended option with a one-line reason — so the human can decide quickly.
- **Feedback loop.** After changes, run the project's tests via the harn
  `run_tests` tool. Do not mark a task complete while tests fail.
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

## Creating a task
When the human describes work in words (or points at a PRD like `prd001`), YOU
author the task file — don't make them format it:
1. **Clarify first.** If the goal, scope, or acceptance criteria are fuzzy, ask
   (expanded `ask_user`) before writing the file. A vague task is a bad task.
2. **Name it by lineage.** `harn_env/tasks/<project>-prd<NNN>-task<NNN>-<slug>.md`,
   where `<project>` is `[harn] project` from `harn.toml` (e.g. `prj001`), the
   PRD matches the one it belongs to, and `<NNN>` is the next free task number
   under that PRD. So the filename alone tells you which PRD to read.
3. **Structure the body** with these fields and sections:
   ```
   # <clear imperative title>

   prd: prj001-prd001      # the parent PRD — read it for the why/scope
   external_id:            # optional: a JIRA/Linear key, for integrations
   status: todo
   priority: <int, lower = sooner>

   ## What
   One or two sentences: the change and its intent.

   ## Done when
   - concrete, checkable acceptance criteria (this is what verify checks).

   ## Skills
   - list the harn skills that will be needed (so the executor loads the right
     ones on demand), e.g. `security`, `standards`.
   ```
4. Keep it small and reviewable; split big asks into several tasks under the PRD.

## Project-specific notes
<!-- Fill in: domain, key commands, anything an agent must always know. -->
