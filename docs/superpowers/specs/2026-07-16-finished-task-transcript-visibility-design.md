# Finished-Task Transcript Visibility Design

## Goal

A task in `review` (or any post-run status) must show its real Agent activity and step results in Studio, not a "Starts when this flow runs" placeholder.

## Problem

`openRunHistory()` only sets `RUN_HISTORY_MODE='execution'` when `BOARD.run.task_id===taskId` — i.e. the task is executing *right now*. A task that already ran to completion (no active run) falls through to `RUN_HISTORY_MODE='preview'`, which is the branch meant for a task that has *never* run: it sources plan nodes from the open canvas preset instead of the task's own frozen snapshot, and `stepTranscriptHtml()` short-circuits on `'preview'` before ever fetching the saved transcript — even though `step_transcript.jsonl` holds the full real history on disk.

The existing code comment ("An active/stopped execution reads its immutable task snapshot") already anticipated covering stopped runs; the condition never implemented it.

## Behavior

- A task that has never run (no `step_results`, no `baseline_ref`, no `task_patch_refs`) still gets `'preview'`: template from the open canvas, "Starts when this flow runs".
- A task that is currently running OR has run before (any of `step_results`, `baseline_ref`, `task_patch_refs` present) gets `'execution'`: its own frozen per-task plan snapshot, and the real transcript fetched and rendered per step — regardless of whether a run is active right now.

## Implementation

In `openRunHistory()`, broaden the `'execution'` condition to also check the selected task's own historical fields (from `BOARD.tasks`), not only `BOARD.run`. No backend or schema changes — the data already exists; only the frontend gating changes.

## Verification

A `test_studio.py` regression test on `studio._HTML` confirming the broadened condition is present (matching this file's existing test pattern for embedded JS behavior), since no live browser harness exists in the test suite for this flow.
