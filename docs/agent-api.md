# Agent API

`harn ui` (the studio) exposes a small set of HTTP endpoints that let an
external system — a script, a bot, another agent — drive harn the same way
the browser UI does. This document covers the three "agent" routes: running
a role, drafting a new role, and (planned) document intake.

All routes take the project's env dir via the `env` query parameter, e.g.
`POST /api/agents/run?env=/path/to/project/harn_env`. If `env` is omitted the
server falls back to the default project it was launched against.

## Auth model

By default `harn ui` binds `127.0.0.1` only, and every route is reachable
with no token — this is the "local operator" setup and needs no
configuration.

If you set `HARN_API_TOKEN` in `harn_env/secrets.env` (`KEY=value` lines,
gitignored, chmod 600), three routes become **protected**:

- `POST /api/agents/run`
- `POST /api/agents/generate`
- `POST /api/tasks/intake`

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

**Not implemented yet.** This route is planned as part of the
document-intake feature (turning an uploaded document into one or more
tasks) and is not callable today — calling it currently returns `404`.

Its auth behavior is already wired ahead of the implementation: it's in
the same protected-route set as `/api/agents/run` and `/api/agents/generate`,
so once it ships it will follow the same Bearer/loopback rules described
above with no further auth changes needed.
