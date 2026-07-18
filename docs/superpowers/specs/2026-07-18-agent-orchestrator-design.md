# Agent Orchestrator Design

## Goal

When a task arrives **without a named role** — `/task <description>` in Telegram, or the HTTP API with `{"text": …}` and no `name` — a cheap LLM turn classifies the work and routes it to the right agent role (per the agent-roles spec), then hands off to the existing `roles_runner.run_role`. When a role IS named explicitly (`/analyst …`), the orchestrator is skipped entirely — current behaviour is unchanged.

## Depends on

- Agent roles spec (`2026-07-18-agent-roles-design.md`) — the orchestrator picks a role and delegates to `roles_runner.run_role`.
- Agent triggers spec (`2026-07-18-agent-triggers-design.md`) — `dispatch_command` / `run_agent_payload` are the entry points the orchestrator hooks into.
- Custom board statuses spec — the deterministic fallback routes to the role servicing the pipeline's first status.

## Problem

Today every Telegram command and every API call must name a role (`/analyst`, `/developer`). A human who just wants "почини флаки-тест" has to know which agent owns that kind of work and which board status it starts in. There is no entry point that says "here is a task, figure out who should do it."

## Behavior

- **New reserved command `/task <description>`** (name configurable via `[orchestrator] command`, default `task`): the orchestrator creates a task from the description (title = first line, description = full text), routes it to a role, sets the task's status to that role's `status:`, and launches the role — the same completion/error reply flows back to Telegram as any other command.
- **API parity**: `POST /api/agents/run` with `{"text": …}` and no `name` routes through the orchestrator (today that body is a "missing name" error). With `name` present, behaviour is unchanged (direct role dispatch).
- **Routing is one cheap LLM turn.** The orchestrator builds a compact prompt: the task description plus a one-line index of every role (`name · services status · first line of the role's ## Role persona`). It never loads full persona bodies — the prompt stays tiny so a cheap model routes accurately and fast. The model returns a single role name.
- **Model selection is AUTO by default.** `[orchestrator] model = "auto"` resolves to the chosen adapter's declared cheap model (`Adapter.CHEAP_MODEL`; `claude` → `haiku`, other adapters fall back to their CLI default until they declare one). A human may override with an explicit model string, and with `[orchestrator] agent` to route on a different adapter than the main chain. Both are editable in Studio Settings.
- **Validation + deterministic fallback.** The returned name is validated against `roles.discover`. An unknown or empty answer (or any LLM failure) falls back to the role servicing the pipeline's first status; if no role matches, the orchestrator returns a clear "no role can service this" error instead of guessing. The routing never raises.
- **Traceability.** The routing decision is recorded two ways: an `orchestrator_route` event (chosen role + resolved model) in `events.jsonl` under the run's `run_id`, and a `record_decision` on the task ("routed to <role> by orchestrator"). Monitoring and the board both show *why* a task went to a given agent.

## Implementation

- `adapters/base.py`: `CHEAP_MODEL: str = ""` class attribute (empty = the adapter's own CLI default, i.e. no `--model` flag). `adapters/claude.py`: `CHEAP_MODEL = "haiku"`.
- `config.py`: `[orchestrator]` section — `enabled` (True), `agent` ("" = main chain), `model` ("auto"), `command` ("task"). Parsed into `Config` fields the same way `[loop]` is; `orchestrator_model`/`orchestrator_agent` added to Studio's settings-save allowlist.
- `orchestrator.py` (new): `route(env_dir, cfg, description) -> str | None` — builds the compact role index, resolves the AUTO model, runs the turn via `loop.get_adapter(cfg.orchestrator_agent or cfg.agent_chain[0])`, parses+validates the name, falls back deterministically, emits `orchestrator_route`. Pure routing — creating/launching the task is the caller's job.
- `triggers.py`: `orchestrate(project_root, env_dir, text, cfg)` — create task → `route` → `set_status(task, role.status, env_dir)` → `dispatch_command(role.command, task.id)`. In `dispatch_command`, `command == cfg.orchestrator_command` delegates to `orchestrate`. In `run_agent_payload`, an empty `name` with non-empty `text` delegates to `orchestrate`.

## Out of scope

- Multi-role decomposition (splitting one description into several tasks/roles). One description → one task → one role.
- Re-routing a task that a role already declined; the existing rework path (oracle FAIL keeps status) handles retries.
- A Studio "orchestrate" input box — the API route exists and is testable; the UI affordance is a follow-up.

## Verification

- Unit: `route` returns the model's choice when valid; unknown/empty/failed turn → deterministic fallback; no roles → `None`.
- Unit: AUTO resolves to `CHEAP_MODEL` and that model reaches `run_turn` (assert on a recording adapter); an explicit `model` string is passed verbatim.
- Unit: `orchestrator_route` event written with chosen role + resolved model.
- Integration: `/task <text>` creates a task (title = first line, status = chosen role's status) and launches that role; API `{"text": …}` without `name` routes through the orchestrator; a body WITH `name` never calls `route`.
