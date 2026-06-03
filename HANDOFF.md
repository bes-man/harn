# HANDOFF — harn

Context handoff from the planning/build session. Read this first, then `README.md`.

## What harn is
An **agent-agnostic** coding harness: a ralph-style autonomous loop with
human-in-the-loop planning, on-demand skills, and a feedback loop. It must work
**identically across Claude Code, Codex, Cursor, and Antigravity**.

## Key decisions (and why)
1. **Brain vs hands split.** All four agents speak **MCP + AGENTS.md + SKILL.md**,
   so all shared logic (tasks, skills, `ask_user`, feedback, notifications) lives
   in ONE MCP server (`harn/mcp_server.py`) and behaves the same everywhere.
   Only the headless entrypoint differs per agent → a thin **adapter** each
   (`claude -p`, `codex exec`, `cursor-agent`, Antigravity CLI/SDK).
2. **Do NOT build on the Claude Agent SDK as the core.** It would lock us to one
   agent. The Claude adapter just shells out to `claude -p` (no SDK dependency).
3. **harn drives the loop, not the agents' native hooks.** Planning, the test
   feedback signal, and "stop and ask when unsure" are enforced by harn so
   behavior is uniform regardless of agent.
4. **Skills = progressive disclosure.** Each md is a `SKILL.md` with frontmatter.
   Only the lightweight index is always in context; bodies load on demand via
   the `read_skill` tool. This is the token-economy requirement.
5. **`harn setup` scaffolds `harn_env/`** into a target project from bundled
   templates (`harn/templates/` = the "base project"; fork + edit to ship your
   own defaults). Local edits are never clobbered on re-run. AGENTS.md is moved
   to the project root; per-agent MCP configs are generated.
6. **Feedback is language-agnostic:** project sets `[feedback] test_cmd` in
   `harn_env/harn.toml`. harn's OWN tests use pytest.
7. **MCP transport:** stdio by default (most local), `--http` flag for later
   remote use. Same tools either way.
8. **Notifications:** Telegram/Slack via env vars; fires when the loop blocks.

## State machine
`PLANNING → READY → EXECUTING → (BLOCKED → wait for `harn answer` → resume) → DONE`.
The agent signals a block via the `ask_user` MCP tool or by writing
`harn_env/state/BLOCKED.md`; harn pauses, notifies, and waits.

## Current state (v0.1, all verified working)
- Working: scaffold, config, tasks/PRD parsing, on-demand skills, state machine,
  feedback runner, notifications, MCP server (stdio + http, 7 tools), **Claude
  adapter**, the loop, and 12 passing pytest tests.
- Verified live: `setup → status → ask_user → BLOCKED → answer → resume`.

## Next steps (priority order)
1. Implement the three stub adapters in `harn/adapters/` — only `run_turn()`:
   `codex exec`, `cursor-agent`, Antigravity (`google.antigravity` SDK / CLI).
2. Idle-watcher daemon: re-notify after `[notify] idle_minutes` while BLOCKED.
3. Wire the `ask_user` MCP tool path end-to-end with a real agent run.
4. Web board: FastAPI + SSE over `harn_env/state` + tasks; create PRDs/tasks
   from the UI. (Frontend can come later.)

## Map of the code
- `harn/cli.py` — setup / run / answer / status / mcp
- `harn/scaffold.py` — `harn setup`
- `harn/loop.py` — the ralph loop + state machine
- `harn/mcp_server.py` — FastMCP server (the universal layer)
- `harn/adapters/` — base + claude (working) + codex/cursor/antigravity (stubs)
- `harn/{state,tasks,skills,feedback,config,notify}.py` — shared pieces
- `harn/templates/` — base project (skills, harn.toml, AGENTS.md, prd/task)
- `tests/` — pytest suite
