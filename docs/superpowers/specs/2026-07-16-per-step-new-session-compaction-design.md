# Per-Step new_session / use_task_context Design

## Goal

Let a workflow step declare a deliberate context-compaction boundary in Studio's builder, so a task's growing context (from spec B) can be summarized and selectively carried forward into a fresh session, instead of every step silently inheriting (or missing) an ever-growing pile of raw history.

## Depends on

Spec B (`2026-07-16-task-context-md-and-capture-design.md`) — this design compacts and reads the `## Context` section that spec B introduces. Not implementable independently.

## Behavior

Two new step fields, same pattern as the existing `tool_mode`:

- `new_session: bool` (default `false`) — Studio shows this as an off-by-default toggle on every step.
- `use_task_context: bool` (default `true`) — only shown/active in the UI when `new_session` is on for that step.

Steps without `new_session` (the default — most steps) get **no** automatic Context injection; each step's prompt is built exactly as it is today.

When a step has `new_session=true`, before it runs:

1. **Compact.** One LLM call, using that step's own configured agent/model (the same `_step_overrides`/`_adapter_for_step` resolution already used to run the step itself), summarizes the task's `## Context` section **incrementally** — only the raw span since the last compaction marker (or since the task's first entry, if this is the first compaction). Earlier compacted spans are untouched.
2. **Replace in place.** The summarized span replaces the raw span it covers with one entry: `### [compacted @ <ts>] covers steps <ids>` followed by the summary text. This is a full-file rewrite of `<task_id>.md` — acceptable because `new_session` steps are opt-in and infrequent, unlike the high-frequency raw append spec B optimizes for.
3. **Forward, if asked.** If `use_task_context=true` on this same step, the just-written compacted entry (only the latest one, not the task's whole history) is injected into this step's own prompt as a new section, e.g. `## Context from earlier in this task`.

Compaction is best-effort: if the summarization call fails, the step still runs without it — matches the existing "never let a side-call crash the turn" convention already used for the oracle/reconcile turns elsewhere in `loop.py`.

## UI

No new panel. Studio's "Context loaded" (spec B, already reading `<task_id>.md`'s `## Context`) naturally shows compacted entries alongside raw ones — they're just entries with a distinct heading.

## Implementation

- Add `new_session`/`use_task_context` to the step node schema (workflow.py/workflows.py), following `tool_mode`'s precedent.
- Studio step editor: two toggles, second one only rendered when the first is on.
- `loop.py`: before running a `new_session` step, call a new compaction helper (task-context module from spec B) that resolves the last compaction marker, dispatches the summarization turn, and rewrites the file. Then, if `use_task_context`, add the compacted section to `_build_step_prompt`'s parts.

## Verification

- Unit tests for incremental compaction (first compaction vs. compacting again only the new span; earlier compacted spans untouched).
- Unit test that a step with `new_session=true, use_task_context=false` compacts and saves but does NOT inject into its own prompt.
- Unit test that a failed compaction call doesn't block the step from running.
- Studio `_HTML` regression tests for the two toggles (matching the existing `tool_mode` test pattern).
