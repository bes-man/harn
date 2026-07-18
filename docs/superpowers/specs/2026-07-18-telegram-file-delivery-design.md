# Telegram File Delivery Design

## Goal

Let an agent deliver a generated artifact — a report, a diagram, a screenshot — as a real **document or photo in Telegram**, attached to the task it belongs to. The model generates the file, attaches it to the task, and sends it to the configured chat.

## Depends on

- Agent roles / secrets spec (`2026-07-18-agent-roles-design.md`) — `secrets_store` is reused to redact secret values before anything leaves the process.
- Existing per-task attachments (`attachments.py`) and the `save_attachment` MCP tool — already present; this spec adds only the outbound Telegram leg.

## Problem

`TelegramHIL` can send only `sendMessage` (text), and its transport (`_http_post_json`) is `application/x-www-form-urlencoded` — text fields only. There is no way to send a file, and no MCP tool an agent could call to push an artifact to the chat. "Send me the report" currently degrades to a truncated text message (~4096-char Telegram limit).

Attaching a file to a task already works: `save_attachment(task_id, filename, content_b64)` writes into `harn_env/storage/<task_id>/` with path-traversal protection (`attachments._safe_name`). Only the Telegram delivery is missing.

## Behavior

- **New Telegram methods** `send_document(path, caption="")` and `send_photo(path, caption="")` on `TelegramHIL`, using a `multipart/form-data` transport (Telegram requires multipart for file uploads). Telegram's Bot API caps a bot document at 50 MB; oversized files return a clear error, not a silent failure.
- **New MCP tool** `send_attachment_to_telegram(task_id, filename, caption="")`: resolves the file **only** through `attachments.path_for(env_dir, task_id, filename)` (so the agent can send only files already attached to a task under `harn_env/storage/`, never an arbitrary path on disk), picks `send_photo` for image extensions / `send_document` otherwise, and returns ok/error.
- **The full flow** the agent performs:
  1. generate the artifact → `save_attachment(task_id, "report.pdf", <base64>)` (already works),
  2. `send_attachment_to_telegram(task_id, "report.pdf", "Отчёт по PRJ-042")` → arrives as a document in the chat.
- **Trust boundary**: delivery targets only the single configured `chat_id` (the user themselves — same boundary as HIL), never a recipient chosen by task content.
- **Secret redaction**: the caption is scrubbed of any secret VALUES from `secrets.env` before sending (reusing `secrets_store.load(env_dir).values()`), so a role's injected secret can't leak into a Telegram caption.

## Implementation

- `telegram.py`:
  - `_http_post_multipart(url, fields, file_field, filename, file_bytes, timeout)` — manual multipart encoder (its own boundary; still stdlib `urllib`, no new dependency), mirroring `_http_post_json`'s error handling (`_warn_once` on TLS/network failure, returns parsed JSON or None).
  - `TelegramHIL.send_document(path, caption="")` / `send_photo(path, caption="")` — read the file, size-check (≤ 50 MB), call the multipart poster against `sendDocument` / `sendPhoto` with `chat_id` + `caption`. Return the message_id or None.
  - A small `_redact(text, env_dir)` helper (or reuse a shared one) applied to the caption.
- `mcp_server.py`: `send_attachment_to_telegram(task_id, filename, caption="")` tool — `attachments.path_for` guard (missing file → clear error), `TelegramHIL.from_env(env_dir)` (unconfigured → clear error), `is_image` chooses the method, logs the send. Never raises to the agent.

## Out of scope

- Sending a file to any chat other than the configured one.
- Auto-attaching a run's result as a file with no explicit tool call (the orchestrator's "report on completion" flow can call this tool, but automatic delivery is a separate decision).
- Inline images inside a text message, media groups, albums — one file per call.

## Verification

- Unit: the multipart encoder produces a well-formed body (boundary present, `chat_id`/`caption` fields, the file part with filename and raw bytes).
- Unit: `send_document` / `send_photo` hit the correct API method; a > 50 MB file returns an error without a network call.
- Unit: the MCP tool rejects a non-existent attachment and any name that escapes the task's storage dir; an unconfigured Telegram returns a clear error.
- Unit: a secret value present in `secrets.env` is redacted from the caption before the poster is called (assert the value is absent from the sent fields).
