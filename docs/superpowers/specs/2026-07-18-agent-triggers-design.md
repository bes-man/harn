# Agent Triggers Design

## Goal

Three ways to start an agent role's run (roles per the agent-roles spec): a Telegram slash command, a task entering the role's status (auto mode), and a local HTTP API call. All three converge on the same launch path.

## Depends on

- Agent roles spec (`2026-07-18-agent-roles-design.md`).
- Custom board statuses spec.

## No new daemon

Everything lives in the existing `harn watch` loop (single instance per project — the pidlock guard already enforces that) and the existing Studio HTTP server. The Telegram bot is the one already configured; slash commands are the router between roles.

## Telegram commands

`watch` already polls `getUpdates` for HIL answers. Extend the same poll:

- `/<command> <arg…>` where `<command>` matches a role's `command:` field.
- Argument routing is deterministic, no LLM: if the argument matches the task-id pattern (`ids.is_tracker_key`) and exists on the board → launch the role on that task. Otherwise the whole argument text becomes a NEW task's description (title = first line, status = the role's `status:`) and the role launches on it. Missing/unknown-id argument → reply with a short usage/error message.
- The run's completion callback (success summary from `## Result`, or failure reason) is sent to the chat, replying to the command message. Questions the run raises flow through the existing BLOCKED → Telegram card mechanism unchanged.
- Commands from chats other than the configured `chat_id` are ignored (same trust boundary as today's HIL).

## Status watch (auto mode)

Each watch tick additionally scans the board: a task whose status is serviced by a role with `trigger: auto`, unclaimed, with no active run → launch. Single-runner constraint holds: if a run is active, candidates simply wait for a later tick (the board IS the queue — no separate queue state to corrupt). Tasks that repeatedly fail keep the existing attempts cap (`_MAX_STEP_ATTEMPTS` persistence), so auto mode cannot burn budget in a crash loop.

## API

Studio HTTP gains `POST /api/agents/<name>/run` with body `{"task_id": "..."}` or `{"text": "..."}` — the exact code path of the Telegram command (shared function, two thin entry points). Localhost-only like the rest of Studio; auth for remote exposure is explicitly out of scope. This is also the door for external systems (a support tool creating a task carrying an `external` frontmatter block and immediately dispatching the analyst).

## Studio surface (minimal)

The board's task panel shows a "Run as <role>" button per role whose `status:` matches the task's current status (manual trigger parity with Telegram). A full agents-management tab is out of scope.

## Verification

- Unit: command parsing (id vs text vs empty), unknown command ignored, foreign-chat ignored, task creation fields correct.
- Unit: auto-scan launches exactly one run, respects claims/active-run/attempt caps; manual-trigger roles are never auto-launched.
- Unit: API endpoint validates role name and body, shares the command code path (assert same function called).
- Callback test: completed run posts summary as a reply; failed run posts the failure.
