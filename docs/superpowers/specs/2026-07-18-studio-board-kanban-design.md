# Studio Board: Kanban Redesign

## Goal

Turn Studio's board from a vertical status-grouped list with a side inspector panel into a real Kanban board — one horizontal column per status, drag-and-drop cards that change status on drop, and a task detail **modal** that surfaces (and lets you edit) everything human-readable in a task file: description, metadata, status, labels, flow/agent, comments, activity.

## Depends on

- Custom board statuses spec (`2026-07-18-custom-board-statuses-design.md`) — columns come from `tasks.lifecycle(env_dir)` / `board_payload`'s `statuses` list (already implemented).
- No dependency on the roles/triggers/orchestrator specs; this is purely the task board's own data + UI.

## Current state (what changes)

- `renderBoard()` (`studio.py`) renders a **vertical list grouped by status** (`.boardgroup` sections stacked top-to-bottom), skipping empty groups. No drag-drop; status changes go through a `<select>` in the side panel (`changeTaskStatus`).
- Task detail lives in a **persistent side panel** (`#insp`, `renderTaskDetail()`), not a modal — already shows: description, workflow, attachments (upload/preview/delete), pipeline dots (live), context-loaded chips, scratchpad, decisions, review log, run log.
- `pollBoard()` refetches `/api/board` and re-renders every 1.5s — fine for a list, but would fight a drag gesture and any in-progress modal edit if untouched.
- The `Task` dataclass / frontmatter has no `labels` field and no human-comment mechanism (`review_log`/`decisions` are agent-authored lifecycle events, not conversation).

## Behavior

### Columns
- One column per configured status, in `lifecycle(env_dir)` order (payload's `statuses`, already shipped). **Empty columns are shown** (unlike today's list, which hides them) — a Kanban board's whole value is seeing the full pipeline at a glance.
- Column header: label + card count. Board scrolls horizontally if columns overflow; each column scrolls vertically independently.
- A filter bar above the board: multi-select by label.

### Cards
- Compact: `id: title`, colored label chips, priority, `claimed_by`/agent, a live-run indicator (reuse the existing `.live-dot`), small comment/attachment counts.
- `draggable="true"`, native HTML5 Drag & Drop API (`dragstart`/`dragover`/`drop`) — no new dependency, matches Studio's self-contained-vanilla-JS constraint (no CDN, no bundler).

### Drag-and-drop → status change
- Dropping a card on a column optimistically moves it client-side, then calls the existing `POST /api/tasks/status`; on failure the card snaps back with the server's error surfaced (`alert`, matching today's `changeTaskStatus` failure path).
- A card belonging to the currently active run cannot be dragged (mirrors the existing server-side refusal in `set_task_status_payload` when a run is active for that task) — visually: not draggable, cursor reflects it.
- **New setting** `[board] launch_on_drag_in_progress` (default `false`): dropping a card into `in_progress`
  - `false` (default): status changes only — same as dropping into any other column. No agent is started; the human still clicks Launch explicitly (in the modal). This is a deliberate behavior change from today's `<select>`, which currently launches immediately — the setting lets a project opt back into the old behavior.
  - `true`: reproduces today's `<select>`-driven behavior — the drop atomically calls `set_task_status_payload`'s existing launch-and-rollback path (unconfirmed flow / active-run conflict → card snaps back with the same error surfaced).
- **Poll/drag interaction**: a module-level `DRAGGING` flag (same pattern as the existing `NEW_TASK_OPEN`/edit-in-progress guards) suppresses `renderBoard()` during an active drag so `pollBoard`'s 1.5s refresh can't yank a card out from under the user's cursor or clobber an optimistic move mid-flight.

### Task detail modal
- Replaces the side panel's role as the primary detail view; opens on card click, closes on backdrop click / Esc / explicit close button.
- **Editable**: `title`, `description`, `priority`, `workflow`, `status`, `labels` — via a new `POST /api/tasks/update` (partial update; only submitted fields change). Status can also change here (same code path as drag).
- **Read-only metadata**: `id`, `epic`, `user_story`, `prds`, `depends_on`, `subtasks`, `claimed_by`, `external` (the tracker-mirror block from the custom-statuses spec, when present).
- **Actions**: Launch / Launch (auto) / Stop, "Edit this task's plan", "Open flow" — unchanged from today's buttons, relocated into the modal.
- **Sections carried over as-is**: Attachments (upload/preview/delete — unchanged), Pipeline (live dots), Context loaded, Decisions, Review log, Run log (while running).
- **New section — Comments/Activity**: a single merged, chronological feed of `review_log` entries (agent lifecycle events — unchanged, read-only) and the new `comments` list (see Data model), each entry tagged by kind (`human` / `hil` / `agent` / `external`) with a distinct visual treatment (e.g. a small kind badge). A text box lets a human post a new comment.

### Comments — behavior
- Posting a comment (`POST /api/tasks/comment`, body `{task_id, text}`) does two things: appends a `Comment` entry to the task's `## Comments` markdown section (persisted, human-authored=`kind: human`), **and** appends the same text to `## Context` via the existing `tasks.append_context` (so the next agent turn actually sees it — comments are a real communication channel, not just a UI log).
- A HIL answer, when recorded (existing `state`/`answer` flow), is *also* appended as a `Comment` with `kind: hil` — so the merged feed shows the full human↔agent conversation in one place instead of splitting it across the blocked-question banner and a separate log.
- The `Comment` shape (`ts`, `author`, `text`, `kind`) is deliberately the same shape a future tracker-sync (GitLab/Linear issue comments) would populate with `kind: external` — no import logic in this spec, shape only, mirroring how the custom-statuses spec's `external` block was scoped.

## Data model

- `tasks.py`:
  - New `Task.labels: list[str]` field, frontmatter round-trip (`_FRONTMATTER_FIELDS` gains `"labels"`, `_render_frontmatter`/`_build_task`/`to_dict`/`create_task` thread it through) — same pattern as the existing `prds`/`depends_on` list fields.
  - New `Comment` dataclass (`ts`, `author`, `text`, `kind`) + `Task.comments: list[Comment]`, a new `## Comments` markdown section parsed/rendered exactly like `## Decisions`/`## Review log` (`_KNOWN_SECTIONS`, `_render_bullets`/`_bullets` reuse, `_build_task`).
  - `add_comment(env_dir, task, text, *, author="user", kind="human")`: appends the `Comment`, saves, and calls `tasks.append_context(env_dir, task.id, step_id="comment", text=...)` with a formatted `[comment by {author}] {text}` line — `env_dir` is a required positional because `append_context` itself requires it.
- `config.py`: `[board] launch_on_drag_in_progress` (default `false`), parsed alongside the existing `[board]` statuses parsing from the custom-statuses spec.
- `studio.py`:
  - `board_payload` already ships full `to_dict()` per task — `labels`/`comments` ride along once `to_dict` includes them (no payload shape change needed beyond the `Task` field additions).
  - `update_task_payload(env_dir, payload)` — partial update: only keys present in `payload` (`title`/`description`/`priority`/`workflow`/`labels`) are applied; `status` is deliberately NOT handled here (stays on the existing `set_task_status_payload` / drag path, avoiding two code paths that can both change status).
  - `add_comment_payload(env_dir, payload)` — thin wrapper over `tasks.add_comment`.
  - New routes: `POST /api/tasks/update`, `POST /api/tasks/comment`. `POST /api/tasks/status` (drag) is reused as-is.

## Out of scope

- Reordering cards within a column (priority stays a numeric field edited in the modal, not drag-reordered).
- Real tracker-comment import (GitLab/Linear) — `kind: external` shape only, per the custom-statuses spec's established pattern.
- Multi-board / swimlanes / per-user views.
- Bulk actions (multi-select cards, bulk status change).

## Verification

- Unit (`tasks.py`): `labels` round-trips through frontmatter; `Comment`/`## Comments` round-trips through the markdown+state split; `add_comment` writes both the Comments section and Context.
- Unit (`studio.py`): `update_task_payload` changes only submitted fields, leaves others untouched, rejects an unknown task id; `add_comment_payload` calls through to `tasks.add_comment`; a HIL answer produces a `kind: hil` comment; `board_payload` includes `labels`/`comments` per task.
- Unit (`config.py`): `launch_on_drag_in_progress` parses, defaults to `false`.
- Unit (`studio.py` `_HTML` regression, matching the existing pattern from the custom-statuses spec): columns render for every configured status including empty ones; drag handlers and the `DRAGGING` guard are present in the served JS.
- Browser (Playwright, per this project's UI-verification convention): dragging a card from one column to another changes its status (assert via the API after drop); with `launch_on_drag_in_progress=false`, dropping into `in_progress` does not start a run; clicking a card opens the modal showing description/labels/metadata; editing title/priority in the modal and saving persists and reflects on the card; posting a comment appears immediately in the modal's feed.
