# AGENTS.md

Core protocol for any agent (Claude Code, Codex, Cursor, Antigravity, Qwen)
working here through **harn**. Situational detail lives in `harn_env/guidance/`
— pull it on demand via `read_guidance(topic)` (index at the bottom). Don't load
guidance you don't need.

## Hard rules (never skip — even for one-line changes)

1. **Load harn's tools first.** They may be deferred (visible by name, not
   callable) when several MCP servers are connected. Your FIRST action each
   session: `ToolSearch(query: "harn", max_results: 30)` (likewise `"context7"`
   / `"semble"` / `"socraticode"` when needed). Deferred ≠ unavailable —
   skipping harn because tools weren't loaded is a violation. If genuinely
   absent, tell the user: "harn MCP is not connected — check .mcp.json and
   restart."
2. **Every code-change request = a harn task.** A chat request ("fix X") is a
   task that doesn't exist yet, not an exemption. No matching task? →
   `create_task` (one sentence is fine) → `get_next_task` to claim it.
3. **Skills on EVERY request.** `list_skills` + `read_skill` the relevant ones,
   and NAME them in your reply ("Loaded: frontend, standards"). An edit that
   ignores a project standard is a latent bug.
4. **Plan mode by default** for anything non-trivial (>1 file, any ambiguity, a
   behavior change): enter your client's plan mode and run the pre-task
   protocol there. Code starts only after the plan + questions resolve. Trivial
   one-liners may skip plan mode but never skip skills + task.
5. **Never ask the human in trailing prose.** Any question with options →
   native interactive UI (`AskUserQuestion` / Plan Mode). For ambiguous
   requirements or standards, ALSO `ask_user(question, skill=…)` (persists +
   escalates). See `read_guidance("hil")`.
6. **Test + reconcile before done.** `run_tests` must pass; after
   `submit_for_review` call `reconcile_skills(task_id)` to capture what you
   learned. Never mark complete while tests fail.

## Pre-task protocol — mandatory, in order, BEFORE any code

Ambiguity found while coding is 10× costlier than ambiguity resolved now.

1. **AS IS** — how it works today. Start from the service registry
   (`list_services` → `read_service` only what the task touches), then code
   search. State current behavior in 2-3 sentences.
2. **TO BE** — target behavior per task + PRD. The AS IS → TO BE delta is your
   scope. Can't state it crisply? That's an ambiguity for step 5.
3. **Skills** — `read_skill` every relevant skill and NAME them. No skill for a
   domain you touch? → `ensure_skill(domain)` (frontend, backend, api, testing,
   security, accessibility, performance, database) or extend the closest via
   `save_to_skill`. Never implement a domain task with zero guidance.
4. **Best practices** — verify against CURRENT practice, not training data:
   context7 (`resolve-library-id` → `get-library-docs`) for the libraries
   you'll touch; code search for in-repo precedent.
5. **Clarify** — list remaining ambiguities (scope, naming, UX, data, edge
   cases, trade-offs). Any exist? → interactive UI + `ask_user`, then STOP.
   None? → say "no ambiguities" explicitly, then implement.

## Loop (chat mode)

`get_next_task` → pre-task protocol → implement → `run_tests` →
`submit_for_review` → `reconcile_skills` → next `get_next_task`. Report each
step in the chat. The oracle runs out-of-band (`harn watch`) — poll the task's
`review_log` and relay its verdict. Don't shell out to `harn run` from chat
(that nests a second agent).

## Guidance index — read on demand (`read_guidance("<topic>")`)

- **services** — service registry: responsibility/standards/constraints per module.
- **code-search** — semble + SocratiCode; search before reading files.
- **hil** — asking the human: per-client interactive UI, ask_user, Telegram.
- **parallel** — running independent tasks across multiple agents.
- **design** — design-before-code (HTML mockup) for user-facing tasks.
- **browser** — verifying UI in a real browser via Playwright.
- **onboarding** — bringing harn up to speed on a new/sparse project.
- **tasks** — authoring tasks; carrying context between iterations; the test gate.

## Project-specific notes
<!-- Fill in: domain, key commands, anything an agent must always know. -->
