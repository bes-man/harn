# Design: chat-mode rework (agreed)

Driven by real-test feedback. The core problem: harn worked only headless
(`harn run`); in a chat (Cursor/Claude Code/Codex) Telegram, status, planning,
oracle and the loop did nothing, because all orchestration lived in the
`harn run` process and the chat has no such process.

## Model: two roles

```
AGENT (hands)                         harn watch (dispatcher)
- Cursor / Claude Code / Codex /      - one lightweight process per project
  headless claude -p                  - runs always; writes NO code
- get_next_task → write code → MCP    - reads state every N s
- submit_for_review → immediately     - BLOCKED.md → interactive Telegram card
  takes the next task (never stalls)    + escalation + cross-channel
- REPORTS EVERY STEP into the chat    - submitted → runs ORACLE headless,
- watches the headless oracle and       writes verdict to the task
  streams its status/verdict to chat  - keeps a LIVE status feed (online)
                                      - advances the loop
```

Principle: `harn watch` does everything that does **not** require writing code
in the IDE. The agent does only the work. Same dispatcher for every agent.

### Decisions
1. **Oracle**: `watch` runs it headless (`claude -p`); writes interim status to
   state; the **agent reads it and streams to the chat** so it's visible live.
2. **Visibility**: the agent reports every step in the chat; `harn watch` shows a
   live feed in the terminal (lightweight process, not Docker).
3. **watch is the chat-mode companion** — documented; not a daemon/Docker.

## Knowledge capture (the point of harn)

harn accumulates project knowledge so the agent decides better over time.

- **No skill hub / library.** Simpler: when the agent learns something important
  (from the user's answer, or discovers a convention in the code), it **saves it
  into the matching skill** via `propose_skill_update(skill, content)` →
  user confirms (HIL) → the skill grows.
- **Planning is a structured dialog**, one question at a time (HIL card / chat
  with options + recommendation), not a wall of text. Each answer fills a PRD,
  a task, or a skill.
- Loop closes: future tasks read the grown skills → the agent asks less, decides
  more (autonomy becomes meaningful).

## Clean project root (hard rule)

harn writes content **only** under `harn_env/`. The project root must not collect
harn files unless strictly required by an agent's own discovery rules:

| File | Must live | Why unavoidable |
|------|-----------|-----------------|
| `.cursor/mcp.json` | `.cursor/` | Cursor only looks there |
| `.mcp.json` | root | Claude Code only looks there |
| `AGENTS.md` | root | agents read it from root |

Rules:
- Create an MCP config **only for the agent(s) in the chain** — never both by
  default. No `harn.sh`, no stray `.md`.
- Anything harn does add to the root is **auto-added to `.gitignore`**.
- `harn teardown` removes the root connectors cleanly.

## Phases
- **Phase 0** — setup health-check (verify MCP starts + tools respond; print exact
  Cursor/Claude enable steps); clean root + auto-gitignore + `harn teardown`.
- **Phase 1** — `harn watch` dispatcher: live status, Telegram from chat, oracle
  headless streamed to chat, loop never stalls (single agent, continuous).
- **Phase 2** — structured planning + knowledge capture (propose_skill_update,
  save-what-you-learn into skills).
- **Later** — N parallel agents (git worktrees); remote skill hub.
