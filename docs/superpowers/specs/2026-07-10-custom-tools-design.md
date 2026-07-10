# Custom tools: upload, agent-chat authoring, and export (Phase 5)

## Problem

Today harn's "Tools" catalog is strictly the ~35 hardcoded `@mcp.tool()` Python
functions in `harn/mcp_server.py` — read-only, nothing a user can add. There is
no way for a user to give an agent a new callable capability (e.g. "create a
Jira ticket", "run our custom linter", "query the staging database") without
editing harn's own source. Users want to:

1. **Add a new tool by uploading a script** (name/description/params entered
   manually, the uploaded file becomes the tool's command body).
2. **Add a new tool by describing it to an agent in a real back-and-forth
   chat** — the agent helps shape the tool's name/description/parameters/
   command, the user reviews a draft, and clicking Save persists it.
3. **Export a tool to share it with another harn user**, and **import** a tool
   someone else exported.

Tool names must never collide — with each other, or with the ~35 built-in
tool names.

## Non-goals

- Arbitrary agent-authored Python code that gets `exec`'d — a custom tool's
  body is always a shell command/script run via `subprocess`, the exact same
  execution model `Type: command` workflow steps already use (Phase 2). No new
  code-execution primitive.
- Live tool-list refresh inside an already-open agent session. MCP clients
  fetch the tool list once at connection time; a newly saved custom tool
  becomes callable only in the agent's *next* session (see "Registration"
  below). This is an MCP-protocol limitation harn cannot work around, not a
  harn design gap — it must be communicated in the UI, not hidden.
- A general-purpose streaming chat UI (message deltas, markdown-as-you-type,
  etc.). The "chat" for authoring a tool is turn-based: each user message
  triggers exactly one blocking agent-CLI subprocess call with the full
  transcript so far as its prompt, and the reply is appended once it returns —
  the same one-shot-per-turn model harn already uses everywhere else, just
  invoked repeatedly with accumulated context. No websockets, no partial
  tokens.

## Design

### Data model — `harn_env/tools/<name>.json`

A custom tool is one small JSON file, mirroring `harn_env/skills/` in spirit
(plain, diffable, single-file-portable) but structured (not prose) since a
tool needs a real parameter list:

```json
{
  "name": "create_ticket",
  "description": "Create a Jira ticket for the given title/body",
  "params": ["title", "description"],
  "command": "jira create --title {title} --body {description}",
  "source": "chat"
}
```

- `name`: the MCP tool name the agent will see (`[a-z0-9_]+`, matching the
  existing skill/tool naming convention already enforced elsewhere in harn).
- `params`: ordered list of simple string parameter names. The agent calls
  the tool with named arguments matching these; FastMCP generates a typed
  signature from this list (mirrors the "one function per registered tool"
  shape `mcp_server.py`'s existing 35 tools already use).
- `command`: a shell command template. `{param_name}` placeholders are
  substituted with `shlex.quote(value)` before the command runs — never raw
  string interpolation, so no argument can break out of its own token
  (the same substitution discipline `harn/feedback.py`'s existing command
  execution already follows).
- `source`: `"upload"` or `"chat"` — provenance only, for display; does not
  affect execution.
- `harn/tools.py` (new module, mirrors `harn/skills.py`'s shape): `discover`,
  `save`, `read`, `delete`, `index` functions over this directory.

### Creation path 1 — Upload

- Studio's Tools tab gains a "＋ Upload tool" button, reusing the existing
  base64-file-upload wiring already built for task attachments
  (`harn/attachments.py` + the hidden-`<input type=file>`/`uploadPickedFile`
  JS idiom in `harn/studio.py`).
- The uploaded file becomes the `command`'s script body (saved alongside the
  JSON as `harn_env/tools/<name>.sh` or `.py`, referenced by the JSON's
  `command` field as e.g. `bash <path> {arg1} {arg2}` — same shlex-quoted
  substitution as the inline case).
- The user fills in `name`/`description`/`params` in a small form alongside
  the upload — these are NOT inferred from the file.

### Creation path 2 — Agent chat

- A "＋ New tool (describe to agent)" panel: a message-history pane + a text
  input + Send button, next to a live-updating draft preview
  (name/description/params/command) and a **Save** button.
- Each Send: harn runs ONE agent turn (the project's configured default
  adapter, same `_run_turn`-style subprocess call already used everywhere
  else in loop.py) whose prompt is the ENTIRE chat transcript so far plus an
  instruction to (a) reply conversationally and (b) emit the current best
  draft tool definition as a fenced JSON block the backend parses out and
  shows in the preview pane. The transcript and the current draft are held
  in memory for the studio session (not persisted until Save — an abandoned
  chat leaves no `harn_env/tools/` file).
- **Save** persists whatever is currently in the preview pane (the user can
  hand-edit the draft before saving — the chat is a drafting aid, not the
  source of truth once a draft exists).
- If the agent's reply doesn't parse into a valid draft (e.g. mid-conversation
  clarifying question), the preview pane simply keeps showing the last valid
  draft (or stays empty before the first one) — Save is disabled until a
  valid draft exists.

### Execution + registration

- At MCP server startup (`build_server`), after registering the ~35 built-in
  tools, harn scans `harn_env/tools/*.json` and registers one dynamically-
  generated `@mcp.tool()` function per definition — each one runs `command`
  (with `{param}` substitution) via `subprocess.run`, capturing stdout/stderr/
  exit code, returning them to the agent exactly like a `Type: command` step's
  result today.
- **MCP protocol limitation (must be surfaced in the UI, not silently
  eaten)**: the tool list is fetched once when an agent's MCP client connects.
  A tool saved mid-session is invisible until that session ends and a new one
  starts. After a successful Save, studio shows: "Saved. This tool will be
  available to the agent starting its next session."

### Uniqueness

- At Save time (both creation paths, and Import), harn checks the new name
  against: (a) the ~35 built-in tool names (`mcp_server.tool_catalog()`'s
  keys), (b) every existing file under `harn_env/tools/*.json`. A collision
  blocks the save with a clear message naming the conflicting tool — the user
  renames before saving, never silently overwritten.

### Export / Import

- Each custom tool in the Tools tab gets an "Export" action: downloads its
  `harn_env/tools/<name>.json` (and its script file, if the tool came from an
  upload, bundled — a single `.json` for chat-authored tools with an inline
  `command`, or a small zip containing both the `.json` and the script for
  upload-authored tools, so Export always produces exactly one shareable
  file).
- "＋ Import tool" accepts that same file (json or zip), runs the SAME
  uniqueness check as Save, and on success writes it into the recipient's own
  `harn_env/tools/`.

## Testing

- `harn/tools.py`: save/read/delete/index round-trip; JSON shape validation
  (rejects a definition missing `name`/`command`).
- Uniqueness: saving/importing a tool whose name collides with a built-in
  tool, or with an existing custom tool, is rejected with the conflicting
  name in the error — for both Save (upload + chat) and Import.
- Command substitution: `{param}` placeholders are `shlex.quote`d, not
  raw-interpolated (a param value containing `; rm -rf /` must not execute as
  a second command — assert the quoted value appears literally in the
  executed argv).
- Dynamic registration: `build_server()` exposes a saved custom tool via
  `mcp.list_tools()`/`tool_catalog()` alongside the 35 built-ins; calling it
  actually runs `command` and returns captured stdout.
- Agent-chat drafting: each Send dispatches exactly one agent turn with the
  full transcript as prompt; a reply containing a valid fenced JSON draft
  updates the preview pane; an invalid/missing draft leaves the previous
  draft (or empty state) untouched, and Save stays disabled until a valid
  draft exists.
- Export: downloads the exact file(s) needed to reconstruct the tool
  (round-trip test: export then import into a second `harn_env`, confirm the
  reconstructed tool behaves identically).
- Upload: an uploaded script becomes the tool's command body and runs
  correctly with substituted params.
