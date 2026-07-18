# Document Intake → Tasks Design

## Goal

Let a document sent to harn become a task with the file attached, optionally routed to an agent role, with an **explicit confirmation gate** before the agent starts working. Two intake channels: a file uploaded to the Telegram bot, and an HTTP multipart upload. This is the "send the bot a document → it files a task, confirms, then works" half of the end-to-end scenario (the "send a result back" half is the already-written Telegram file-delivery spec).

## Depends on

- Agent roles + triggers specs — a document-triggered task launches through the same `triggers.dispatch_command` / `roles_runner.run_role` path.
- Existing attachments (`attachments.save`, path-traversal-safe) — the received file is stored as a normal task attachment the agent reads via `read_attachment`.
- Telegram HIL (`await_answer`) — reused for the confirmation gate.
- Authenticated API spec (`2026-07-19-push-pr-and-authenticated-api-design.md`) — the HTTP intake route sits behind the same Bearer-token gate.

## Problem

harn cannot receive a file at all today: `TelegramHIL` only reads text updates, and there is no upload endpoint. So "send the bot a report and have it act on it" is impossible — the closest is manually creating a task and hand-attaching a file in Studio.

## Behavior

### Telegram file upload
- The existing `getUpdates` poll (the same one `poll_commands` drains) additionally recognizes `message.document` and `message.photo` from the trusted `chat_id`.
- On receipt: download the file (`getFile` → file path → download), create a new task, and attach the file via `attachments.save`. The message `caption` becomes the task title (first line) + description.
- If the caption starts with a slash command matching a role's `command:` (or the orchestrator's `/task`), the document-triggered task is routed to that role; otherwise it lands as a plain task in the pipeline's first status (no agent auto-runs).
- Foreign-chat uploads are ignored (same trust boundary as HIL/commands).

### HTTP multipart upload
- `POST /api/tasks/intake` — multipart body: `file` (required), `text` (optional description), `agent` (optional role name). Behind the Bearer-token gate (auth spec). Saves the attachment, creates the task, and — if `agent` is given — routes it through `triggers.dispatch_command`. This is the door for external systems (a support tool POSTing a ticket export).

### Explicit confirmation gate
- A document-triggered task that WILL run an agent first posts a confirmation before launching: a short summary — "About to run **<role>** on '<title>' with **<filename>** — confirm?" — sent through the existing HIL card (Telegram) / surfaced in Studio, and waits for a yes via `await_answer`.
- Governed by `[intake] confirm_before_run` (default `true`). `false` skips the gate (fully autonomous intake). A decline cancels the run and leaves the task sitting in its landing status with the file attached (nothing lost).
- A plain intake with no `agent` (no run requested) never needs a gate — there is nothing to confirm.

## Implementation

- `harn/telegram.py`: `download_file(file_id) -> bytes | None` (`getFile` + download, best-effort like the rest), and extend the poll drain to surface `{document|photo}` updates from the trusted chat (a `poll_documents`, sibling to `poll_commands`, same offset file so the two never reprocess each other).
- `harn/intake.py` (new): `intake(env_dir, project_root, *, filename, data, text, agent=None, cfg) -> dict` — `attachments.save` → `tasks.create_task` (title = first line of `text`/caption, description = full text) → if `agent`: run the confirmation gate (unless `confirm_before_run=false`), then `triggers.dispatch_command`. One code path both channels share.
- `harn/loop.py` (watch): the per-tick command scan also drains document updates and calls `intake.intake`.
- `harn/studio.py`: `POST /api/tasks/intake` multipart handler → `intake.intake`, behind the auth gate.
- `harn/config.py`: `[intake] confirm_before_run` (default `true`).

## Out of scope

- Parsing the document's CONTENTS (OCR, PDF/text extraction) — the file is attached and the agent reads it on demand via `read_attachment`; no server-side content extraction.
- Media groups / albums / multiple files per message — one file per intake.
- Non-Telegram chat channels (Slack etc.) — Telegram + HTTP only.

## Verification

- Unit (`intake.intake`): a file + text creates a task with the attachment saved and title/description set; with `agent` set and `confirm_before_run=false`, it dispatches; with the gate on, it awaits confirmation and a decline does NOT launch (task remains in landing status, attachment intact); unknown `agent` returns a clear error without creating a dangling run.
- Unit (`telegram.download_file` + poll): a stubbed `getFile`/download yields bytes; a document update from the trusted chat is surfaced, a foreign-chat one is ignored.
- Unit (studio): `POST /api/tasks/intake` parses multipart, calls `intake.intake`, and is refused without a valid Bearer token when one is configured (auth spec).
- Unit (`config`): `confirm_before_run` parses, defaults `true`.
- Integration-style: a Telegram document with caption `/analyst reproduce this crash` creates a task, attaches the file, and (gate off) launches the analyst role on it.
