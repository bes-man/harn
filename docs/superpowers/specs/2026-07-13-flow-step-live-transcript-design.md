# Flow step live transcript

## Goal

The Flow run sidebar must show a persistent, human-readable live transcript for
each workflow step. The experience should resemble the visible Codex chat feed,
but every message and operation must belong to exactly one task step.

The existing progress summary remains useful, but `PRJ-044 · 1/1 complete`
must never be the only evidence of a completed agent step when the agent
produced messages or operations.

## User experience

- Clicking `RUN WORKFLOW` opens the selected task's actual workflow plan.
- Every step has its own transcript beneath its title and status.
- The active step opens automatically. New entries appear without replacing
  the whole sidebar, collapsing cards, or resetting the user's scroll.
- Completed and failed steps retain their transcript across page reloads and
  harn restarts.
- Inactive completed steps are collapsible. Pending steps remain compact.
- Visible entry types are:
  - agent message;
  - status or reasoning summary intended for the user;
  - command execution and result;
  - MCP/tool or skill invocation and result;
  - file-change summary;
  - error.
- Raw adapter JSONL and hidden chain-of-thought are never rendered.
- Existing green/yellow/red tool and skill requirement indicators remain and
  are augmented by the live invocation entries.

## Architecture

### Adapter event boundary

`Adapter.run_turn` gains an optional event callback. Adapters emit a small,
provider-neutral event structure while the turn is running:

```text
kind       message | status | command | tool | skill | file_change | error
phase      started | updated | completed | failed
title      short human-readable label
text       visible content or summarized result
item_id    provider item id, when available
timestamp  UTC timestamp
```

Codex uses `codex exec --json` and reads stdout line by line. Current Codex
JSONL items for agent messages, reasoning summaries, command execution, file
changes, MCP calls, collaboration calls, and web searches are normalized at
this boundary. Turn usage is still returned through `AgentResult`.

Adapters without structured streaming keep their current execution path and
emit coarse lifecycle entries plus the final visible response. This makes the
feature useful for every configured agent without pretending all providers
offer the same detail.

### Persistent transcript

The loop supplies the current `task_id`, `step_id`, and run id to the callback.
Normalized entries are appended to a dedicated JSONL transcript under
`harn_env/state`. Each record contains its task, step, run, sequence number,
and normalized visible event.

Transcript persistence is separate from `task.step_results`:

- `step_results` remains the compact execution ledger and usage audit;
- transcript JSONL is the append-only detailed history;
- a new attempt starts a new attempt scope rather than overwriting the old
  transcript;
- the UI displays the latest attempt by default and can retain previous
  attempts as collapsed history when they exist.

Writes are append-only and best-effort. A telemetry write failure must not
fail the agent turn.

### Studio API

A read-only endpoint returns transcript entries for one task and optionally
one step. It filters server-side and returns only normalized fields. The
existing board poll can include a transcript revision/cursor; the sidebar
fetches only entries after its last sequence number while open.

No WebSocket or SSE is required. The existing 1.5-second polling cadence is
retained, which keeps the stdlib HTTP server architecture and is sufficient
for the current Studio interaction model.

### Sidebar rendering

The browser stores transcript entries keyed by `step_id`. Rendering updates
only the affected transcript container. Existing nodes, disclosure state, and
scroll positions are preserved.

Entry presentation:

- messages use readable prose blocks;
- status/reasoning summaries use subdued commentary styling;
- commands and tool calls use compact labelled rows with expandable results;
- errors use the existing red failure language;
- an active item has a subtle live indicator;
- empty running steps show `Waiting for agent output…`, not an empty card.

The final `AgentResult.text` is emitted as a message only if an equivalent
agent-message item was not already streamed, preventing duplicate results.

## Data and lifecycle rules

1. A step is marked running and its attempt scope is created.
2. The adapter process starts and emits normalized entries as output arrives.
3. Each entry is persisted before it becomes visible through the API.
4. Studio polls by cursor and appends entries to the matching step.
5. On turn completion, usage and final status are saved in `step_results`.
6. The transcript remains readable after completion, failure, block, browser
   reload, or Studio restart.
7. Retry creates a new attempt. The newest attempt opens automatically; prior
   attempts are not mixed into it.

## Error handling

- Malformed provider JSONL is ignored as a structured event but retained in
  diagnostic logs; it does not terminate the turn.
- Adapter stderr is converted to a visible error only when it represents an
  actual failure, not routine provider diagnostics.
- A transcript API failure leaves the existing entries visible and retries on
  the next poll.
- Unknown future provider item types are skipped safely.
- Transcript size is bounded per API response and read incrementally by cursor.

## Testing

- Adapter unit tests feed representative Codex JSONL and assert normalized
  message, reasoning-summary, command, tool, result, error, and usage events.
- Streaming tests use a delayed fake process and prove an entry is persisted
  before process completion.
- Loop tests prove entries are tagged with the correct task, step, run, and
  attempt and survive a completed or failed turn.
- API tests prove task/step filtering and cursor pagination.
- Studio tests prove that polling appends to the correct step without replacing
  the sidebar or resetting disclosure/scroll state.
- Playwright verifies two delayed steps: the first transcript updates live,
  completion preserves it, and the second step receives a separate transcript.

## Non-goals

- Rendering hidden chain-of-thought.
- Displaying raw provider JSONL in the normal sidebar.
- Replacing the existing harn event/metrics stream.
- Introducing WebSockets, SSE, or a frontend framework.
- Guaranteeing identical detail from providers that do not expose structured
  streaming output.
