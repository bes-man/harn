# Task Context Markdown + Real Capture Design

## Goal

Replace name-only context tracking ("read_skill was called") with the actual captured content the agent held, stored as one human-readable file per task — future-compatible with syncing to an external tracker (GitHub issues) via API.

## Problem

Two gaps, discovered together:

1. **No content capture.** `adapters/claude.py`'s `_normalize_event()` only processes `type: "assistant"` stream-json records (model text, and tool-call *names* with `text: ""`). `type: "user"` records carrying `tool_result` content (the actual return value of every tool call — a skill body, a weather JSON, anything) are silently dropped. Studio's "Context loaded" panel can therefore only ever say a skill/service/prd/guidance *name* was read, never show what it contained.

2. **No place to keep it that scales.** The task's own JSON (`tasks._save()`) is rewritten in full on every save (many times per run); `tasks.board()` — injected into nearly every step's prompt — parses every task file in full just to print one summary line per task. Embedding a growing context blob directly in that JSON would make both operations cost grow with total accumulated context across every task in the project.

## Format

One new file per task: `harn_env/tasks/<task_id>.md` — the **human-facing** record, replacing today's `<task_id>.json` for these fields. Fixed section order:

```markdown
---
id: PRJ-044
title: Узнать погоду и курс доллара
status: review
priority: 100
workflow: test
prds: []
depends_on: []
---

## Description
...

## Result
...

## Decisions
- ...

## Review log
- 2026-07-13T16:19Z submitted_for_review by agent: ...

## Context
### step-45fe0a — Check rate (2026-07-13 16:16:09)
[captured tool_result / model message content]
```

`## Context` is the **last** section and is append-only for the task's lifetime.

Engine bookkeeping that the human never needs to read — `step_results` (per-step usage tracking for required-tool/skill enforcement), `stage_checkpoints` (git refs for rollback), `task_patch_refs`, `claimed_by`/`claimed_at` (parallel-worker claim lock), `spec_locked`, `subtasks`, `epic`, `user_story`, `skills` — stays in a small separate sidecar, `harn_env/tasks/<task_id>.state.json`, exactly the role `<task_id>.workflow.json` (unchanged, stays separate — it's a structured node array edited by Studio's visual canvas, not prose) already plays today. `scratchpad` is dropped: `## Context` supersedes it.

## Write patterns (why this doesn't regress board()/save() cost)

- `tasks.board()` reads **only the frontmatter** (from the first `---` to the second) — bounded, independent of how large `## Context` has grown.
- Frontmatter/Description/Result/Decisions/Review log changes are infrequent (a few times per step) and go through a full-file read-modify-write, same cost class as today's JSON save.
- `## Context` entries are appended via a true OS-level file append (`open(path, "a")`) — O(1) regardless of file size — because this happens far more often (every streamed tool_result/message within a turn, potentially dozens of times per step), not just per-step.

## Capture mechanism

Extend `ClaudeAdapter._normalize_event()` to also emit an event for `type: "user"` records containing `tool_result` blocks, resolving the originating tool's name via the `tool_use_id` ↔ `tool_use.id` correlation already visible in the same stream (track a small id→name map across one turn's events). `loop._run_turn()`'s `on_event` — already the single choke point every step type (sequential, parallel-wave member, on_fail handler) passes through — appends message/tool_result content to `<task_id>.md`'s Context section, capped per entry (~4000 chars, matching the existing `step_results.output` truncation convention) so a noisy tool (e.g. a full test-suite dump) can't flood it; each capped entry gets a "(truncated)" marker so it's visible, not silent.

The existing `_context_read()` mechanism (tags `read_skill`/`read_service`/`read_prd`/`read_guidance` calls for required-usage enforcement) is unchanged — different concern (audit/enforcement, not content).

## Migration

Existing `<task_id>.json` files are upgraded lazily: the first time `tasks.find()`/`load_tasks()` reads a task, if only the old JSON exists, split it into the new `.md` + `.state.json` pair and remove the old file. No separate migration command; no dual-format runtime support needed once every task has been touched once.

## Scope note

This is a wide-reaching but mechanical refactor: `tasks.py`'s load/save layer, its dataclass-to-file mapping, and every test that constructs a task via `conftest.make_task` or asserts on `<task_id>.json` directly will need updating. Flagging this now so the implementation plan sizes it correctly — the design itself is a single coherent change, not several independent ones.

## Out of scope (this spec)

- Actual GitHub issue sync (this only prepares a shape compatible with it later).
- `PROGRESS.md`'s own unbounded-growth/full-file-read-for-tail issue (separate, filed for later).
- Studio's "Context loaded" panel UI change to render this file (small follow-up once the file exists).

## Verification

- Unit tests for the new adapter-level `tool_result` capture (id→name correlation, truncation, cap marker).
- Unit tests for `.md` read/write/append round-trip, and for `board()` reading only the frontmatter (a large Context section must not slow down or appear in `board()`'s parsed output).
- Migration test: an old-format `<task_id>.json` is read once and produces the correct `.md` + `.state.json` split, old file gone.
- Full existing test suite green after the `tasks.py` refactor.
