# harn_example — what `harn setup` creates

This is a **filled-in sample** of the `harn_env/` that `harn setup` scaffolds
into your project. It exists so you can see exactly where the harness stores its
context. In a real project the directory is named `harn_env/` (not
`harn_example/`).

```
harn_env/
├─ harn.toml                # agent(s), test_cmd, loop + notify settings
├─ skills/<name>/SKILL.md    # on-demand "skills" (only the index is always in context)
│   ├─ project/             # what this project is, conventions
│   ├─ architecture/        # how it's structured
│   ├─ standards/           # coding standards
│   ├─ constraints/         # hard limits the agent must respect
│   ├─ security/            # security rules
│   └─ ui/                  # UI/UX guidance
├─ prd/<name>.md             # product requirement docs (status, problem, scope)
├─ tasks/<name>.md           # the backlog — one task per file, walks the track
│   ├─ EXAMPLE.md           # a fresh `todo` task
│   └─ 0002-jwt-auth.md     # a `done` task: see its Review log + Notes for future agents
├─ state/                    # runtime state (created/updated by the loop)
│   ├─ STATE.json           # loop phase, current task, iterations
│   ├─ PROGRESS.md          # append-only history every agent reads on pickup
│   ├─ ANSWERS.md           # answers you gave to blocking questions
│   ├─ BLOCKED.md           # (transient) the question the agent is blocked on
│   └─ .telegram_offset     # (transient) Telegram long-poll cursor
└─ mcp_snippets.md           # ready-to-paste MCP config for Codex & Antigravity
```

Siblings created at the **project root** (not inside `harn_env/`):

- `AGENTS.md` — portable, always-on instructions read by every agent.
- `.mcp.json` — MCP server config for Claude Code.
- `.cursor/mcp.json` — MCP server config for Cursor.

The `state/` files here are illustrative. On a clean `harn setup`, `state/`
starts empty and fills in as you run the loop.
