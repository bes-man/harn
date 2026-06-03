# harn

Agent-agnostic coding harness with a ralph-style feedback loop, on-demand
skills, and human-in-the-loop planning. Works with **Claude Code, Codex,
Cursor, and Antigravity** through one shared MCP server.

## The idea

harn separates the **brain** (portable) from the **hands** (per-agent):

- **Brain — identical across agents.** All four agents speak MCP, `AGENTS.md`,
  and `SKILL.md`. So harn's capabilities — tasks, on-demand skills, `ask_user`,
  the feedback loop, notifications — live in *one* MCP server and behave the
  same everywhere.
- **Hands — a thin adapter per agent.** Only the headless entrypoint differs
  (`claude -p`, `codex exec`, `cursor-agent`, Antigravity CLI/SDK). Each adapter
  is a few lines. Claude is implemented; the others are stubs.

The ralph loop is driven by harn itself (not by each agent's native hooks), so
planning, the test-feedback signal, and "stop and ask when unsure" work
uniformly regardless of which agent runs.

## Install

```bash
pipx install .            # or: pip install -e .   (Python 3.10+)
```

`harn mcp` additionally needs the `mcp` package (declared as a dependency).

## Quickstart

```bash
cd /path/to/your/project
harn setup                # scaffolds harn_env/ + AGENTS.md + per-agent MCP config
# edit harn_env/harn.toml -> [feedback] test_cmd = "pytest -q"   (your project's tests)
# add tasks in harn_env/tasks/*.md, PRDs in harn_env/prd/*.md
harn run                  # runs the loop with the configured agent
harn status               # see phase; if BLOCKED, it's waiting on you
harn answer "use postgres"   # unblock, then `harn run` again
```

## Layout

```
harn_env/                 # created in your project by `harn setup`
  harn.toml               # agent, test_cmd, loop + notify settings
  skills/<name>/SKILL.md   # the md "skills" (project, standards, security, ...)
  prd/                     # product requirement docs
  tasks/                   # backlog (status: todo|done, priority: N)
  state/                   # STATE.json, BLOCKED.md, ANSWERS.md
AGENTS.md                  # portable always-on instructions (project root)
.mcp.json / .cursor/mcp.json   # generated MCP config; snippets for Codex/Antigravity
```

The bundled templates in `harn/templates/` are the **base project**. Fork harn,
edit those, and every `harn setup` you run ships your defaults. Local edits in a
project's `harn_env/` are never clobbered on re-run.

## MCP transport

`harn mcp` serves over **stdio** by default (the agent spawns it as a
subprocess — fully local, no ports). Run `harn mcp --http --port 8765` to serve
over `127.0.0.1` instead; that same server can later sit behind auth/TLS for
remote use. Same tools either way.

## Notifications

Set env vars and harn pings you when the agent is blocked:
`HARN_TELEGRAM_BOT_TOKEN`, `HARN_TELEGRAM_CHAT_ID`, `HARN_SLACK_WEBHOOK_URL`.

## Status (v0.1)

Working: scaffold, config, tasks/PRD, on-demand skills, state machine, feedback
runner, notifications, MCP server (stdio + http), Claude adapter, loop, tests.
Stubs to implement next: Codex / Cursor / Antigravity adapters, the idle-watcher
daemon, and the web board.

## Tests

harn's own tests use pytest (a target project can use any test command):

```bash
pip install -e . pytest
pytest -q
```
