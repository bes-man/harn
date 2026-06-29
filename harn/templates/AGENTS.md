# AGENTS.md

Core protocol for any agent (Claude Code, Codex, Cursor, Antigravity, Qwen)
working here through **harn**. Situational detail lives in `harn_env/guidance/`
— pull it on demand via `read_guidance(topic)` (index at the bottom). Don't load
guidance you don't need.

## Hard rules (never skip — even for one-line changes)

1. **Load harn's tools first, then the workflow.** Tools may be deferred (visible
   by name, not callable) when several MCP servers are connected. Your FIRST
   action each session: `ToolSearch(query: "harn", max_results: 30)` (likewise
   `"context7"` / `"semble"` when needed). Then `read_workflow` (the
   always-followed flow + required skills per step) and follow it. Deferred ≠
   unavailable. If absent: "harn MCP is not connected — check .mcp.json."
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
   escalates). When resuming, call `check_pending_answer()` first — the answer
   may have arrived via Telegram. See `read_guidance("hil")`.
6. **Test → verify → reconcile (never skip).** `run_tests` must pass. Then
   verify EACH `## Done when` criterion against the actual implementation (not
   just test output). Then `submit_for_review`; then `reconcile_skills(task_id)`
   to capture learnings — this is how harn accumulates project standards; skipping
   it is a protocol violation. Check `board()` for the oracle verdict and relay it.
   Never mark done while tests fail.

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
5. **Clarify (the funnel)** — narrow, don't enumerate. Ask the
   highest-leverage question first (the one that collapses the most options),
   via interactive UI + `ask_user`, one at a time; after each answer drop the
   ruled-out branches. When nothing material is left open, `lock_spec(task_id,
   done_when, approach, decisions)` — the verified minimal spec the executor
   implements verbatim. After lock, the full PRD is read-on-demand (`read_prd`),
   not re-read. No ambiguities at all? say so, lock, implement.

## Loop (chat mode)

`get_next_task` → pre-task protocol → implement → `run_tests` →
**VERIFY** (re-read `## Done when`, check each criterion against the actual code —
not just test output; fix gaps or call `ask_user` for human decisions; end with
`VERIFY: PASS` or `VERIFY: FAIL`) →
`submit_for_review` →
`reconcile_skills(task_id)` (read the brief, call `save_to_skill` for confident
conventions and `ask_user(skill=…)` for trade-offs; end with `RECONCILE: DONE`) →
**oracle check**: call `board()` to read the oracle verdict once `harn watch` runs
it; relay PASS / FAIL / DEBT to the user; FAIL returns the task to
`changes_requested` →
next `get_next_task`. Report each step in the chat.

**Log significant changes** (when `[log] changes` is on, the default): after a
meaningful piece of work, `record_change(task_id, summary, detail)` — ONE
release-notes line per change (not a per-edit diary). Reconcile backstops it;
docs come from `generate_changelog`.
Don't shell out to `harn run` from chat (that nests a second agent).

**Bidirectional answers:** after `ask_user`, if the human replies with an
ambiguous message ('ok', 'continue'), call `check_pending_answer()` before
`answer_question` — the reply may have come via Telegram while you were stopped.
`harn watch` auto-starts and handles Telegram escalation (after
`chat_grace_minutes`, default 5 min) and idle-silence notifications.

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
