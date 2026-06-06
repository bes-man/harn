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
  feedback runner, notifications, MCP server (stdio + http, 7 tools), **all four
  adapters**, **Telegram HIL (wait-for-reply)**, the loop, and 39 passing pytest
  tests.
- Verified live: `setup → status → ask_user → BLOCKED → answer → resume`.

## Next steps (priority order)
1. ✅ DONE — the three stub adapters in `harn/adapters/` now implement
   `run_turn()`: `codex exec`, `cursor-agent -p`, `antigravity exec`. Shared
   subprocess handling lives in `Adapter._run_cli` (base.py). Tests in
   `tests/test_adapters.py`. NOTE: verify the real Antigravity headless syntax
   (`antigravity exec` is a best guess) and swap one line in its `run_turn` if
   different.
2. ✅ DONE (mostly) — Telegram human-in-the-loop in `harn/telegram.py`:
   `TelegramHIL.wait_for_reply` posts the blocking question and long-polls
   `getUpdates` for the human's answer, re-reminding every `[notify]
   idle_minutes`. Wired into the loop (`loop._await_telegram_answer`) behind
   `[notify] wait_for_reply`; on reply it auto-resumes via `loop.answer`.
   Tests: `tests/test_telegram.py`, `tests/test_loop_hil.py`. This subsumes the
   old "idle-watcher daemon" item (reminders happen inside the wait). A
   standalone background daemon (notify while harn isn't actively running) is
   still open if you want re-notifies without an active `harn run`.
3. Wire the `ask_user` MCP tool path end-to-end with a real agent run (the
   pieces — MCP `ask_user`, BLOCKED.md, Telegram wait, `harn answer` — all
   exist; this is a live integration test against `claude -p`).
4. Web board: FastAPI + SSE over `harn_env/state` + tasks; create PRDs/tasks
   from the UI. (Frontend can come later.)

## Added since handoff (task lifecycle + multi-agent + loop-aware)
- **Per-task review track** (`harn/tasks.py`):
  todo→in_progress→review⇄changes_requested→done. Review thread + acceptance
  notes are appended to each task `.md`. `submit_for_review` / `request_changes`
  / `accept` / `board`. `next_task` resumes in_progress first, then rework, then
  new. CLI: `harn review <id> --approve [--notes] | --changes "…"`, `harn board`.
- **Review gate in the loop** (`harn/loop.py` `_review_gate`): on tests-pass the
  task goes to `review`; harn waits for the human in Telegram (`approve …` →
  accept+notes; anything else → changes) or, without Telegram, notifies and waits
  for `harn review`. New loop phase `state.REVIEW`.
- **Loop-aware prompts** (`loop._build_prompt`, default `[loop] loop_aware`):
  every prompt carries the task board, `PROGRESS.md` tail, and prior answers, so
  any agent has full context and you can watch the track from inside the agent.
- **Multi-agent** (`[harn] agents = [...]`, `Config.agent_chain`,
  `loop._pick_adapter`): first installed agent in the chain runs; all share the
  same file/MCP context. `HARN_AGENTS` env override.
- **Telegram default-on + sleep-safe**: `wait_for_reply` defaults true; the
  offset-based long-poll picks up answers sent while the machine slept.
- Docs: `README.md` (EN+RU, single entry point), `FLOW.md` (mermaid lifecycle +
  sequence), `harn_example/` (a filled-in sample of the `harn_env/` structure).
- Tests: `tests/test_lifecycle.py`, `tests/test_loop_review.py` (52 total).
- NOTE: there's a known design choice — in CLI (non-Telegram) review mode the

## Added since (verify + tokens + tool cleanup + Qwen)
- **Qwen Code adapter** (`harn/adapters/qwen.py`, `qwen -p`) — fifth agent.
- **Verify step** (`[loop] verify`, default true; `loop._build_verify_prompt` /
  `_verify_verdict`): after tests pass, a dedicated verification turn checks the
  work against the task's acceptance criteria, fixes small gaps, or calls
  `ask_user` when a human decision is needed. New phase `state.VERIFYING`. Block
  handling refactored into `loop._check_block` (reused by work + verify turns).
- **Token usage** (`AgentResult.input_tokens/output_tokens/cost_usd`,
  `usage_str`): Claude adapter parses `--output-format json` (with safe fallback
  to raw text). The loop accumulates per-task usage and records it in the Review
  log + `PROGRESS.md`. Other agents report nothing until their adapters parse it.
- **MCP tool cleanup**: `complete_task` → `submit_for_review` (sets `review`, no
  longer lets the agent bypass the human review gate by marking `done`);
  `status` → `loop_status` (avoid confusion with an OS/service healthcheck).
- **Expanded ask_user guidance** (`_ASK_GUIDANCE` in prompts + `ask_user`
  docstring + AGENTS.md template): questions must include context/why, options
  with trade-offs, and a recommendation.
- Tests added: `tests/test_verify.py`, `tests/test_tokens.py` (63 total).

## Added since (autonomous mode + chat protocol)
- **Autonomous mode** (`harn run --auto`/`-a`, `[loop] auto` + `auto_max_iterations`,
  default 30): no human — the agent researches best practices and decides itself
  (`_AUTO_NOTE` / `_AUTO_DECIDE_NOTE` injected into prompts), over a larger budget.
  **Never mutates harn_env `.md`** (status/PROGRESS/ANSWERS/review all gated by
  `if not auto`); handled tasks tracked in-memory via `tasks.next_task(exclude=)`.
  Blocks are auto-cleared instead of waiting. Not for complex tasks.
  `loop._handle_block` (was `_check_block`) and `loop._run_verify` are auto-aware.
  Tests: `tests/test_auto.py` (68 total).
- **Interactive chat protocol** (AGENTS.md "Two ways harn runs"): in a chat the
  agent self-drives via MCP tools and asks the human directly in-chat — it must
  NOT shell out to `harn run` (nesting). Documented; no code path needed since
  `harn run` headless and chat-driven use the same MCP tools.

## Added since (chat-grace escalation + cross-channel HIL)
- **Channel + chat grace** (`[notify] channel` chat|telegram|both, default both;
  `chat_grace_minutes` default 5; env `HARN_HIL_CHANNEL`, `HARN_CHAT_GRACE_MINUTES`).
  A blocking question waits in the chat first, then escalates to Telegram after
  the grace. Modelled on staidy's `hil_chat_grace_sec` / `_maybe_delayed_telegram_notify`.
- **Cross-channel resolution**: answer in either place. The "chat" channel is
  detected by the BLOCKED marker being cleared (via `harn answer` or the MCP
  `answer` tool). `TelegramHIL.await_answer()` returns `(reply, source)` with
  source `telegram`|`chat`|`''`; on a chat answer landing after the card was
  posted it edits the card (`TelegramHIL.edit_message`). `wait_for_reply` kept as
  a back-compat wrapper (telegram-only, no grace).
- **`harn watch`** (`loop.watch`): HIL coordinator for chat-driven runs (no
  `harn run`) — escalation fires regardless of where the agent runs.
- `loop._await_telegram_answer` → `loop._await_answer` (returns `(reply,source)`,
  channel/grace aware). Tests: `tests/test_hil_escalation.py`, updated
  `tests/test_loop_hil.py` (73 total).
- STILL TODO (user: "improve later"): multi-session routing — several concurrent
  agent sessions, replies matched by Telegram reply-to/inbox. Port staidy's
  `hil_store.py` (SQLite: sessions/inbox/open_sessions) + `telegram_hil.py`
  `_session_for_reply_to` / `_route_text_to_session`. Today: one pending Q at a time.

## Added since (project+PRD task naming)
- **Canonical id scheme** `prj001-prd001-task001` (new `harn/ids.py`, ported from
  staidy `scripts/task_ids.py`). Tasks are files
  `tasks/<project>-prd<NNN>-task<NNN>-<slug>.md`; PRDs `prd/<project>-prd<NNN>-<slug>.md`,
  so each task name carries its project + PRD lineage.
- `tasks.Task` gained `code` / `project` / `prd` (parsed from the stem; `None`
  for plain back-compat names), plus **`prd_ref`** (the parent PRD, from an
  explicit `prd:` line, else the filename) and **`external_id`** (from an
  `external_id:` line — e.g. a JIRA key, for future integrations).
  `tasks.find()` resolves by full stem, canonical code, or bare `task001` (when
  unambiguous). `board()` shows the code + prd. `[harn] project` config (default
  `prj001`) is the source of the project code.
- `loop._build_prompt` injects a "Task lineage" block (which PRD to read +
  external_id); MCP `get_next_task` returns `prd=` / `external_id=` lines.
- **Task authoring flow** (AGENTS.md "Creating a task"): when the human describes
  work in words or points at a PRD, the agent clarifies (ask_user), then writes
  the task file itself — named by lineage, with `prd:`/`external_id:` fields,
  `## What` / `## Done when`, and a `## Skills` list of skills the executor needs.
- Renamed bundled templates + `harn_example/` to the scheme; added the `prd:`
  lineage + `external_id:` fields and a filename-convention comment.
  Tests: `tests/test_task_ids.py` (87 total).
- NOTE: id stays = filename stem (no blast radius); code/project/prd are derived.
  A `harn task new` generator (auto-number using `[harn] project` + ids helpers)
  is the obvious next convenience — not built yet.

## Added since (planning turn + oracle)

### Planning turn (`[loop] planning`, default true)
Before any code is written, the first agent turn for a **new task** is a planning
pass: agent reads task + PRDs, calls `ask_user` for every ambiguity, then calls
`update_task` MCP tool with refined acceptance criteria. Code starts on the NEXT
iteration. Skipped on rework (`changes_requested`) and in `--auto` mode.
- `loop._build_planning_prompt()` — PLANNING PHASE instructions
- MCP `update_task(task_id, description, skills, prds, priority)` — agent writes
  refined spec back into the JSON task file
- `ReviewEntry.event = "planning_started"` marks that planning happened (used to
  detect is_first_touch on subsequent iterations)

### Oracle (`[loop] oracle`, default false; `oracle_agent = ""`)
After verify passes, an **independent agent with fresh context** checks:
1. Are acceptance criteria genuinely met (not just superficially)?
2. What technical debt was introduced?

Verdict on its own line:
- `ORACLE: PASS` → proceeds to human review
- `ORACLE: FAIL — reason` → loops back for rework (adds `oracle_fail` log entry)
- `ORACLE: DEBT — description` → proceeds to review with debt note surfaced to
  human (adds `oracle_debt` log entry)

`oracle_agent = ""` → same agent as main (fresh subprocess = fresh context).
Set to a different agent name for true model diversity.
- `loop._build_oracle_prompt()` — oracle instructions + task spec + git diff
- `loop._oracle_verdict()` — parses ORACLE: PASS/FAIL/DEBT
- `loop._git_diff()` — best-effort `git diff HEAD` for the oracle
- `loop._run_oracle()` — runs oracle, handles verdict, logs to task JSON
- `loop._pick_oracle_adapter()` — picks oracle agent from config

### On parallel agents
"Parallel" in practice = oracle is a second agent with fresh context (no shared
conversation history). True parallel execution (two agents building independently
then merging) requires git worktrees + conflict resolution — out of scope for now.

Tests: `tests/test_planning.py`, `tests/test_oracle.py` (99 total).

## Added since (continuity + code search + rollback + i18n)
- **Lightweight continuity (variant 2)**: `Task.scratchpad` (free note, replace) +
  `Task.decisions` (append, `Decision` dataclass) in task JSON. MCP tools
  `set_scratchpad` / `record_decision`. Executor/verify see them as continuity;
  **oracle sees decisions as CLAIMS to verify, scratchpad is hidden** from it.
  Introduced `loop._task_spec()` (renders spec WITHOUT runtime fields) — replaced
  raw-JSON injection so scratchpad never leaks as a requirement. `accept` clears
  scratchpad (decisions kept). Tests: `tests/test_continuity.py`.
- **Code search (variant C)**: `harn/semble_bridge.py` — semble (Python, bundled
  dep, `>=0.1.0,<1.0`) + SocratiCode (npx `socraticode@^1.8`, Docker+Qdrant).
  `[code_search] semble/socraticcode` (both default true). Oracle priority:
  SocratiCode `codebase_impact` (static) > semble `find_related` (semantic) >
  diff-only. Diff-scoped via `changed_files()`. scaffold adds available servers
  to MCP configs. NOTE (Habr 1043774): MCP schema "start tax" ~7-8k tok/session ×
  fresh processes — accepted because per-search savings dominate; revisit only if
  measured. `docs/architecture.svg` (RU) shows the layering.
- **i18n**: PRD section headings are language-flexible (`prd._SECTION_ALIASES`:
  Проблема→Problem, Цель→Goal, Критерии приёмки→Acceptance criteria). All harn
  parsers are Cyrillic-safe. semble's default model is English-tuned → AGENTS.md
  tells agents to search by code identifiers, not task-language wording.
  Tests: `tests/test_prd.py`.
- **Per-task rollback** (`harn/gitutil.py`, `loop.rollback`, `harn rollback <id>`):
  captures `Task.baseline_ref` (git HEAD) at first execution turn; restores the
  working tree to it (dry-run default; `--apply`; `--reopen` resets task to todo
  and clears scratchpad/decisions/baseline). **Excludes `harn_env/`** so harn's
  own bookkeeping is never clobbered. Tests: `tests/test_rollback.py` (117 total).

## Chat-mode rework (design agreed; in progress)

Real-test feedback exposed that harn only worked headless (`harn run`); in a chat
(Cursor/Claude/Codex) Telegram, status, planning, oracle, and the loop did
nothing. Full design + decisions: `docs/DESIGN_chat_mode.md`. Model:
**agent = hands, `harn watch` = lightweight dispatcher** (Telegram, oracle
headless, live status, loop advance); agent reports every step to the chat;
knowledge capture = agent saves what it learns into skills (no hub); clean
project root.

### Phase 0 — DONE (setup health-check + clean root)
- `scaffold.py`: MCP connector written ONLY for agent(s) in the chain (claude →
  `.mcp.json`, cursor → `.cursor/mcp.json`, codex/antigravity/qwen → snippet in
  harn_env). No more writing both by default.
- Root connectors (`AGENTS.md` + the one MCP config) are auto-added to
  `.gitignore` (`_gitignore_add`). `scaffold.teardown()` removes them.
- `mcp_server.healthcheck(env_dir)` launches `harn mcp` in a subprocess and
  confirms tools respond. `harn setup` prints the result + exact enable steps
  (Cursor needs a manual toggle; Claude `/mcp`).
- New CLI: `harn doctor` (re-check), `harn teardown`.
- Tests: test_scaffold cursor/teardown/gitignore (135 total).

### Phase 1 — DONE (watch dispatcher + visibility + oracle headless)
- `loop.oracle_review(env_dir, cfg, task, project_root)` — oracle extracted into
  a reusable headless function (used by BOTH `harn run` and `harn watch`); writes
  status to PROGRESS, verdict to the task (oracle_pass/fail/debt), and on FAIL
  moves the task to `changes_requested`. `_run_oracle` is now a thin wrapper.
- `loop.watch(env_dir, project_root, _once=…)` rewritten as the dispatcher:
  (1) live status feed (echoes new PROGRESS lines + phase), (2) BLOCKED → Telegram
  card + escalation + cross-channel (+auto button), (3) tasks in `review` without
  an oracle verdict → run oracle headless. Lightweight loop, not a daemon.
- MCP tools log to PROGRESS (`mcp_server._log`) so the dispatcher and the chat
  agent can SEE activity regardless of where work runs (chat or `harn run`).
- `ask_user` no longer sends a one-way push — it records BLOCKED and lets the
  dispatcher/loop turn it into the interactive card.
- AGENTS.md chat protocol: report every step, don't stall after submit, poll the
  oracle verdict in review_log and relay it, keep `harn watch` running.
- Tests: `tests/test_watch.py` (140 total).

### Phase 2 — DONE (knowledge capture + structured planning)
- `skills.append_learning(env_dir, name, content)` — saves a learned fact into a
  skill, creating it if missing (`## Learned (captured from the team)` section).
- MCP `save_to_skill(skill, content, description)` — agent captures durable
  knowledge (confirmed answers, discovered conventions) into skills, so future
  work asks less. Logs to PROGRESS.
- AGENTS.md: knowledge-capture rule ("an answer you don't capture is a question
  you'll ask twice"); planning is now a one-question-at-a-time DIALOG (write
  criteria first, ask singly, capture, repeat — no wall of text); new
  "Onboarding a new/under-specified project" section (fill PRD, ask standards →
  save_to_skill, set test_cmd). (#3, #5, #6)
- Tests: `tests/test_knowledge.py` (145 total).

### Remaining from the feedback
- #5 planning in chat now relies on the protocol; verify live that the agent
  actually does one-question-at-a-time (prompt is there; behaviour needs a real
  run to confirm).
- Later: N parallel agents (git worktrees); remote skill hub (explicitly NOT
  wanted now — capture-into-skills is the chosen model).

## Earlier note
- in CLI (non-Telegram) review mode the
  loop stops at REVIEW for the human; with Telegram it blocks inline per task.

Porting note: a richer, battle-tested Telegram HIL lives in
`/Users/maximus/projects/staidy/harn/hil/` (inline Accept/Cancel buttons,
SQLite store, multi-agent sessions, plan-review phase, staged-reply confirm).
harn's version is a deliberately minimal stdlib adaptation; pull more from there
if/when multi-session or staged confirmation is needed.

## Map of the code
- `harn/cli.py` — setup / run / answer / status / mcp
- `harn/scaffold.py` — `harn setup`
- `harn/loop.py` — the ralph loop + state machine
- `harn/mcp_server.py` — FastMCP server (the universal layer)
- `harn/adapters/` — base (`_run_cli`) + claude/codex/cursor/antigravity/qwen (all working)
- `harn/{state,tasks,skills,feedback,config,notify}.py` — shared pieces
- `harn/tasks.py` — task backlog + the per-task review lifecycle
  (todo→in_progress→review⇄changes_requested→done) and `board()`
- `harn/progress.py` — append-only `state/PROGRESS.md`, the shared cross-agent log
- `harn/telegram.py` — Telegram HIL: post question/review + long-poll for the reply
- `harn/templates/` — base project (skills, harn.toml, AGENTS.md, prd/task)
- `tests/` — pytest suite
