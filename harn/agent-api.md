# Agent API

`harn ui` (the studio) exposes a small set of HTTP endpoints that let an
external system — a script, a bot, another agent — drive harn the same way
the browser UI does. This document covers the three "agent" routes: running
a role, drafting a new role, and document intake (uploading a file that becomes a task).

All routes take the project's env dir via the `env` query parameter, e.g.
`POST /api/agents/run?env=/path/to/project/harn_env`. If `env` is omitted the
server falls back to the default project it was launched against.

## Auth model

By default `harn ui` binds `127.0.0.1` only, and every route is reachable
with no token — this is the "local operator" setup and needs no
configuration.

If you set `HARN_API_TOKEN` in `harn_env/secrets.env` (`KEY=value` lines,
gitignored, chmod 600), every route that can launch agent execution, spend
LLM tokens, or mutate a role becomes **protected**:

- `POST /api/agents/run`
- `POST /api/agents/generate`
- `POST /api/agents/save`
- `POST /api/agents/delete`
- `POST /api/tasks/launch`
- `POST /api/tasks/launch_workflow`
- `POST /api/tasks/run_stage`
- `POST /api/tasks/run_step`
- `POST /api/tasks/rerun_workflow`
- `POST /api/tasks/status` (moving a task to `in_progress` auto-launches a
  run, so this route is gated the same as the explicit launch routes)
- `POST /api/tools/chat` (runs an LLM turn)
- `POST /api/tasks/intake` (multipart file upload → task; see below)

Read-only `GET` info routes (`/api/state`, `/api/board`, `/api/tasks/transcript`,
etc.) are **not** protected — the threat model here is unauthorized action
(launching agents, spending tokens, mutating roles), not information
disclosure, so those stay reachable without a token.

Protected-route requests must carry:

```
Authorization: Bearer <token>
```

matching `HARN_API_TOKEN`, **except** requests from a loopback client
(`127.0.0.1` / `::1` / `localhost`) — those are always exempt, so the local
browser UI keeps working with no token even after you configure one for
remote callers. The `Bearer` scheme match is case-insensitive (`Bearer` or
`bearer` both work), and the token comparison is constant-time.

If `HARN_API_TOKEN` is unset, behavior is unchanged from before this
feature: no token is required from anyone (protection is opt-in).

A request that fails the check gets `401 {"error": "unauthorized"}`.

### Reverse-proxy caveat — read this before binding to a non-loopback host

The loopback exemption assumes `harn ui` receives connections directly, with
no intermediary. **If you front `harn ui` with a reverse proxy** (nginx,
Caddy, an SSH tunnel terminating locally, etc.) that terminates TLS and
forwards to `127.0.0.1`, then **every** proxied request — including ones
from a remote, unauthenticated attacker — arrives at the studio process
looking like it came from `127.0.0.1`. The loopback check can't tell the
difference, so it treats all of them as trusted and the `HARN_API_TOKEN`
check is skipped entirely. In that configuration the token provides **no
protection** for remote callers, silently.

harn does **not** trust `X-Forwarded-For` or similar headers to recover the
real client IP — that header is attacker-controlled and trivially spoofed,
so trusting it would be worse than the current behavior, not better.

If you need `harn ui` reachable from off-box:

- Prefer running it bound directly to the interface you want (not behind a
  loopback-terminating proxy), so real peer IPs reach the loopback check, or
- If a reverse proxy is required, enforce access control **at the proxy
  layer** (mTLS, an auth gate, IP allowlisting, a VPN) — do not rely on
  `HARN_API_TOKEN` to do that job when a loopback proxy sits in front.

### curl example (remote caller, token configured)

```bash
curl -X POST 'http://your-host:9999/api/agents/run?env=/path/to/project/harn_env' \
  -H 'Authorization: Bearer <HARN_API_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"name": "implementer", "task_id": "T-42"}'
```

A call from `127.0.0.1` (e.g. the studio's own browser UI, or a script
running on the same box as `harn ui`) needs no `Authorization` header at
all, whether or not `HARN_API_TOKEN` is set:

```bash
curl -X POST 'http://127.0.0.1:9999/api/agents/run?env=/path/to/project/harn_env' \
  -H 'Content-Type: application/json' \
  -d '{"name": "implementer", "task_id": "T-42"}'
```

## `POST /api/agents/run`

Dispatches a role agent — the exact same code path a Telegram `/<command>`
uses (`triggers.dispatch_command` via `triggers.run_agent_payload`). Runs
the role against an existing task, or creates a new task from free text
first.

**Request body** — one of:

```json
{"name": "implementer", "task_id": "T-42"}
```

```json
{"name": "implementer", "text": "Add a dark-mode toggle to settings"}
```

- `name` (required) — the role's `command:`/`name:` value.
- `task_id` — an existing task id on the board. Takes precedence over `text`
  if both are given.
- `text` — free text; a new task is created from it (title = first line,
  truncated to 120 chars) if `task_id` isn't provided.

One of `task_id` or `text` is required.

**Response** — the role run result:

```json
{"ok": true, "task_id": "T-42", "status": "review", "warning": null, "pr_url": null}
```

`pr_url` is set only when the role has `push: true` and the push+PR step
succeeded. On failure:

```json
{"ok": false, "error": "no agent role 'implementer'"}
```

## `POST /api/agents/generate`

LLM-drafts a role (and an optional workflow) from a free-text description.
**Never persists anything** — the caller reviews the draft and, if they
want to keep it, calls the separate `POST /api/agents/save` route.

**Request body:**

```json
{"description": "A reviewer that checks PRs for test coverage before approving"}
```

**Response:**

```json
{
  "ok": true,
  "draft": {
    "role": {"name": "agent", "status": "review", "next_status": "done", "...": "..."},
    "workflow": {"nodes": [{"kind": "step", "title": "Step 1", "body": "...", "required": [], "tools": []}]},
    "dropped": []
  }
}
```

`dropped` lists any skill/tool names the model referenced that don't exist
in this project (filtered out of the draft). On a missing description:

```json
{"ok": false, "error": "description is required"}
```

## `POST /api/tasks/intake`

Turn an uploaded document into a task with the file attached, optionally
routed to an agent role. `multipart/form-data` body:

- `file` (required) — the document. Rejected if larger than the attachment
  cap (25 MB) with `{"ok": false, "error": "file too large (max 25MB)"}`.
- `text` (optional) — the task description (first line becomes the title).
- `agent` (optional) — a role name/command to run on the new task (subject
  to the confirm gate, below).

Response: `{"ok": true, "task_id": "PRJ-001"}` (plus the run result merged in
when `agent` was given and the run proceeded). A missing `file` →
`{"ok": false, "error": "file is required"}`.

Follows the same Bearer/loopback auth rules as the routes above (it's in the
protected set). curl (loopback, no token needed):

```
curl -X POST 'http://127.0.0.1:9999/api/tasks/intake?env=/path/to/project/harn_env' \
  -F 'file=@report.pdf' \
  -F 'text=Investigate the Safari login bug' \
  -F 'agent=analyst'
```

The same intake path also accepts a **document sent to the Telegram bot**
from the trusted chat: the caption becomes the task text, and a caption that
starts with `/<role>` routes the new task to that role.

### Confirm gate

`[intake] confirm_before_run` (default `true`) governs whether a
document-triggered task that would run an agent asks for confirmation first.
When `true` and an `agent` is given, harn posts a confirmation to Telegram and
waits for a yes before launching; a decline or timeout keeps the task (with
the file attached) without running it. Set it to `false` for unattended
intake that runs immediately.

**Known limitation:** when `confirm_before_run = true` and a *Telegram*
document carries a `/command` (auto-run), the `harn watch` dispatcher blocks
inline on the confirmation for up to the confirm timeout — no other
command/document is processed during that window, and any Telegram message
sent while it waits is consumed and silently dropped. For unattended or
high-throughput Telegram intake, set `confirm_before_run = false`, or confirm
promptly. (The HTTP route blocks only the single request thread, not the
dispatcher.)
