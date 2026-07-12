# harn

Agent-agnostic coding harness with a ralph-style feedback loop, on-demand
skills, human-in-the-loop planning, and a transparent per-task review track.
Works with **Claude Code, Codex, Cursor, Antigravity, and Qwen Code** through
one shared MCP server.

> 📈 Diagrams (task lifecycle + full interaction) — [FLOW.md](./FLOW.md).
> 📁 A filled-in sample of what `harn setup` creates — [harn_example/](./harn_example/).
> 🇷🇺 Краткая версия на русском — [в конце файла](#harn--на-русском).

## The idea

harn separates the **brain** (portable) from the **hands** (per-agent):

- **Brain — identical across agents.** Every agent speaks MCP, `AGENTS.md`,
  and `SKILL.md`. So harn's capabilities — tasks, on-demand skills, `ask_user`,
  the feedback loop, notifications — live in *one* MCP server and behave the
  same everywhere.
- **Hands — a thin adapter per agent.** Only the headless entrypoint differs
  (`claude -p`, `codex exec`, `cursor-agent -p`, `antigravity exec`,
  `qwen -p`). Each adapter is a few lines, and all five are implemented.

Adding a new agent is just a small adapter (`harn/adapters/<name>.py` with one
`run_turn()`) — everything else is shared.

The ralph loop is driven by harn itself (not by each agent's native hooks), so
planning, the test-feedback signal, the review gate, and "stop and ask when
unsure" work uniformly regardless of which agent runs.

## Install

```bash
pipx install .            # or: pip install -e .   (Python 3.10+)
```

`harn mcp` additionally needs the `mcp` package (declared as a dependency).

## Quickstart — let your agent set it up (recommended)

harn is meant to be installed *by the agent you already work with* (Cursor,
Claude Code, Codex). Paste this to your agent in the project:

> **Set up harn in this project.** Run `pip install
> git+https://github.com/bes-man/harn` then `harn setup`. If it tells you to
> enable an MCP server, tell me the exact steps and wait until I confirm it's on.
> Then "onboard this project": read `harn_env/state/ONBOARD.md`, map the code,
> and interview me (one question at a time) to fill the PRD and the standards
> (use `ask_user` with `skill=`). Start `harn watch` in the background. From then
> on, use harn's MCP tools for all work.

Once `harn setup` is done and the MCP server is connected, paste this to start
working on any task:

> **First, confirm your harn MCP tools are loaded** — you must see `ask_user`,
> `answer_question`, `get_next_task` in your tool list. If any are missing,
> stop and tell me so I can fix the connection before we continue.
>
> **Use harn MCP tools for all work in this project. Never ask questions as
> plain chat text** — always use `ask_user(question, skill=…)` so answers are
> saved to skills automatically.
> 1. Call `get_next_task` to pick up the next task from the board.
> 2. During planning, call `ask_user(question, skill=…)` before acting on
>    anything ambiguous. Stop after calling it — I will answer in this chat,
>    then you call `answer_question(answer=…)` to record it and continue.
> 3. Write code. Run `run_tests`. Fix until green.
> 4. Call `submit_for_review` — harn runs the oracle automatically.
> 5. Repeat until `board` shows all tasks done.
> Read `harn_env/AGENTS.md` for the full protocol.

`harn setup` is non-interactive when launched this way: it scaffolds `harn_env/`,
writes the MCP connector for your agent (git-ignored), installs the code-search
backends, checks the MCP server, **then runs onboarding** (detects the stack,
seeds skills, warms the index, writes the onboarding brief). The agent drives the
rest of the dialog in chat.

### Or by hand

```bash
cd /path/to/your-project
harn setup                # scaffold + MCP connector + health-check + onboard
                          #   --no-install (skip backends) · --no-onboard
harn watch                # live status + Telegram routing — keep this open
# then in your agent: "onboard this project"  → fills PRD + standards with you
harn run                  # or run the headless loop
harn board / harn status  # the task track / current phase
```

When a task is ready for you:

```bash
harn review <task_id> --approve --notes "watch the edge case in parse()"
harn review <task_id> --changes "rename the module to auth/"
```

…or just answer in Telegram (see [Human-in-the-loop](#human-in-the-loop-telegram)).

## Configure

`harn_env/harn.toml`:

```toml
[harn]
agent = "claude"                    # claude | codex | cursor | antigravity | qwen
# agents = ["claude", "codex"]      # or several, tried in order (first installed runs)
# model = ""                        # default model for any step that sets none of its own
autonomy = 0.7                      # 0 ask-everything … 1 decide-everything

[feedback]
test_cmd = "pytest -q"              # your project's tests (any language)
require_tests = true                # code change w/o tests → one "add tests" nudge

[loop]
max_iterations = 10
loop_aware = true                   # agent sees the board + progress + lifecycle
oracle = true                       # independent verification turn (blast-radius)
design = true                       # UI tasks: HTML mockup approved BEFORE code
auto = false                        # autonomous (also `harn run --auto`); see below
# Per-step agent/model/effort/temperature now live on the step itself, in the
# workflow plan (set via the studio UI) — not here. `[harn] model` above is
# just the default a step falls back to when it sets none of its own.
max_cost_usd = 3.0                  # run-level cost ceiling, 0 = unlimited
max_tokens = 400000                 # run-level token ceiling, 0 = unlimited
# crossing either STOPS the run and BLOCKS it — change here or in Settings tab
turn_timeout_seconds = 1800         # per-turn subprocess timeout, 0 = adapter default

[browser]                           # Playwright phase: verify criteria in the LIVE app
enabled = false                     # turn on, then re-run `harn setup` (adds the MCP)
app_cmd = "npm run dev"             # how to start the app ("" = already running)
app_url = "http://localhost:3000"   # where it answers

[code_search]                       # search-before-reading; installed by `harn setup`
semble = true                       # semantic chunk retrieval (no infra)
socraticcode = true                 # dependency graph / blast-radius (Docker)

[mcp]
context7 = true                     # live library docs as MCP tools (needs Node.js)
ui_supervise = true                 # `harn ui` supervises an owned `harn mcp --http` child
ui_port = 8765                      # that child's port
tool_reload_seconds = 2             # hot-reload harn_env/tools/ this often, 0 = disable

[notify]
idle_minutes = 30                   # reminder cadence while waiting on you
wait_for_reply = true               # answer & review right in Telegram (+ Auto button)
wait_timeout_minutes = 0            # 0 = wait forever; else fall back to CLI
channel = "both"                    # chat | telegram | both (chat-grace escalation)
```

Secrets come from environment variables, never the file:
`HARN_TELEGRAM_BOT_TOKEN`, `HARN_TELEGRAM_CHAT_ID`, `HARN_SLACK_WEBHOOK_URL`.

## What `harn setup` creates

`harn setup` scaffolds a `harn_env/` into your project (plus a few root-level
files). A filled-in sample lives in **[harn_example/](./harn_example/)** so you
can browse the real thing.

```
your-project/
├─ harn_env/                  # all harness context lives here
│  ├─ harn.toml               # agent(s), test_cmd, loop + notify settings
│  ├─ skills/<name>/SKILL.md   # on-demand "skills"; only the index is always in context
│  │   ├─ project/  architecture/  standards/
│  │   └─ constraints/  security/  ui/
│  ├─ prd/<slug>.md            # product requirement docs (Markdown + frontmatter)
│  ├─ tasks/<id>.json          # backlog — one JSON file per task (AUTH-42 / PRJ-001)
│  ├─ design/<id>.html         # approved UI mockups — the visual contract per task
│  ├─ state/                   # runtime state, written by the loop
│  │   ├─ STATE.json          # loop phase, current task, iterations
│  │   ├─ PROGRESS.md         # append-only history every agent reads on pickup
│  │   ├─ ANSWERS.md          # your answers to blocking questions
│  │   ├─ screenshots/<id>/   # evidence from the browser-verification pass
│  │   ├─ BLOCKED.md          # (transient) the question the agent is blocked on
│  │   └─ .telegram_offset    # (transient) Telegram long-poll cursor
│  └─ mcp_snippets.md          # ready-to-paste MCP config for Codex, Antigravity, Qwen
├─ AGENTS.md                   # portable, always-on instructions (read by every agent)
├─ .mcp.json                   # MCP config for Claude Code
└─ .cursor/mcp.json            # MCP config for Cursor
```

The bundled templates in `harn/templates/` are the **base project**. Fork harn,
edit those, and every `harn setup` ships your defaults. Local edits inside a
project's `harn_env/` are never clobbered on re-run.

## Tasks (JSON) & PRDs (Markdown)

**Tasks are JSON files** in `harn_env/tasks/`, one per task. The filename is the
task **id**:
- with a tracker → use the key: `AUTH-42.json` (`epic`/`user_story` link upward);
- without one → harn auto-numbers `PRJ-001.json`, `PRJ-002.json`… from
  `[harn] project`.

A task references one or more PRDs in a `prds` array (a task may span PRDs), and
carries its own structured `review_log`, `decisions`, and `scratchpad`. You don't
hand-write these — the agent calls the `create_task` / `update_task` MCP tools.

```jsonc
// harn_env/tasks/AUTH-42.json
{
  "id": "AUTH-42", "title": "Add JWT auth", "status": "todo", "priority": 1,
  "prds": ["auth"], "epic": "Q2-SECURITY", "user_story": "AUTH-10",
  "skills": ["security"],
  "subtasks": [{ "id": "AUTH-42-1", "title": "POST /login", "status": "done" }],
  "description": "## What\n…\n## Done when\n- POST /login returns a JWT",
  "review_log": [ /* timestamped events: started, submitted, accepted… */ ]
}
```

**PRDs are Markdown** in `harn_env/prd/<slug>.md` — YAML frontmatter (`id`,
`status`, `priority`) plus sections (`## Problem`, `## Goal`, `## Scope`,
`## Acceptance criteria`). Headings may be in your language (Russian aliases are
recognised). Product/analysts write them; the agent normalises them at loop start.

## The task track (transparent, per task)

Each task's `status` walks a human-visible track; the structured `review_log`
inside the JSON records every step, so the full history travels with the task.

```
todo → in_progress → review ⇄ changes_requested → done
```

1. **todo** — you added it.
2. **in_progress** — an agent is working it (failing tests loop it back here).
3. **review** — the agent finished; harn asks **you** to accept or comment.
4. **changes_requested** — you left a comment; the agent reworks it.
5. **done** — you accepted it; your **notes for future agents** are saved into
   the task.

See [`harn_example/tasks/AUTH-42.json`](./harn_example/tasks/AUTH-42.json)
for a `done` task with its full review log and carried-forward notes.

## Creating and starting tasks from the Board

You don't need an agent session to get a task onto the board:

- **"＋ New task"** on the Board tab creates a task directly from Studio —
  only a title is required; description, flow, and priority are optional and
  default to none/project-default/`10`.
- A task's **status** can be changed right from its detail panel, via the
  dropdown next to the task id (a raw status move — it never touches
  `review_log` or counts as an accept/request-changes).
- **Picking a flow is required before a task can move to `in_progress`.** The
  workflow picker always shows a real, explicit selection — "default
  workflow" counts as one — so re-confirming the default is a valid pick too.
  Moving the status to `in_progress` before any flow has been picked is
  refused with a clear message, and the status stays put.
- Moving a task to `in_progress` **immediately launches its run** in the
  background — the same single-runner-at-a-time rule as the existing
  **▶ Launch** button applies: if another run is already active, the launch
  is refused and the status change is rolled back, so the task never ends up
  stuck at `in_progress` with nothing running.
- **"Open flow ▶"** on the task's detail panel jumps to the Flow tab with
  this task selected, so you can watch it execute live.
- Viewing an unstarted task's plan (or previewing a step's prompt) no longer
  freezes it — a task's real plan is only frozen once something actually
  executes it (a full run, or a single-step **Run**), so switching flows
  before that point genuinely takes effect.

## Workflow steps: agent vs command, on-fail, parallel

`harn_env/WORKFLOW.md` is the plan every task walks (edit by hand or via
`harn ui`'s visual Studio canvas). Each step is a Markdown heading with a few
optional lines that turn plain prose into something `harn run` executes:

- **`Type: command`** — a shell command instead of an agent turn (default
  `Type: agent`). Pair it with **`Command: <shell>`** (runs in the project
  root, no LLM, no tokens) and, optionally, **`On fail: <step title>`** — on a
  non-zero exit/timeout, that target step runs once as a recovery agent turn,
  then the failing command is retried.
- **`Parallel: <group>`** — steps sharing the same group id run concurrently
  instead of one at a time. In Studio, just drag step blocks to the same
  horizontal (Y) level on the canvas — contiguous steps at the same height are
  auto-grouped into a shared group id (shown as a `∥` lane); the `Parallel:`
  field is what actually drives execution, the canvas position is only the
  editing gesture. Under the hood, each step in the group runs in its own
  isolated git worktree checked out from a shared checkpoint; once every
  member finishes, their diffs are merged back into your working tree one at a
  time (a same-file conflict triggers one agent turn to resolve it) — harn
  still never creates commits, so the result is just an ordinary uncommitted
  diff.
- **`On fail:` is not supported inside a parallel group** — combining
  retry/handler-jump semantics with concurrency was left out on purpose.
  Setting both `Parallel:` and `On fail:` on the same step logs a
  `config_error` event and `On fail` is treated as unset for that step (its
  failure is just recorded, not retried through a handler).

## Step observability: skill/tool tiers, usage badges, context preview

- **`Skills (required: a; recommended: b, c)`** and **`Tools (required: x;
  recommended: y)`** — `required` items are always loaded/enforced;
  `recommended` ones are surfaced to the agent but not force-loaded. The old
  bare forms still work exactly as before: `Skills (required: a)` (no
  recommended clause) and `Tools: x, y` (sugar for "all recommended, none
  required").
- After a step runs, Studio's step inspector shows a **green/yellow/red
  badge** on each declared skill/tool chip: green = used, yellow =
  recommended but unused, red = required but unused.
- A **required-and-unused** skill/tool triggers exactly one automatic retry of
  that same step with a reminder appended to its prompt; if it's still unused
  on the retry, harn itself blocks the run (same as any other block) for a
  human to resolve. This enforcement only applies to sequential steps — a
  step running inside a parallel wave still records and shows usage, but is
  never auto-retried or blocked by it.
- The step inspector's **"View full context"** button shows the exact prompt
  a step will receive (or did receive); **"Copy to file"** exports it to a
  plain text file under `harn_env/state/context_exports/`.
- The Board's **"⏸ Pause"** button (renamed from Stop, same underlying stop)
  — steps already marked done stay done; edit the plan and click **▶
  Resume** to continue only the remaining steps.
- When a run is **BLOCKED**, the Board shows the question directly, with a
  text box to answer it, instead of requiring `harn answer` from a terminal
  (it reuses the same `loop.answer()` the CLI calls). Unchanged: if nobody
  answers in time (chat, Studio, or CLI), harn still escalates the same
  question to Telegram after `chat_grace_minutes`.

## Custom tools — extend your agent's toolbox

Beyond the built-in MCP tools, you can add your own **custom tools** the agent
can call. A custom tool is just a **name + description + parameter list + a
shell-command template** with `{param}` placeholders — e.g. a `command` of
`bash lint.sh {path}`. It runs exactly like a `Type: command` workflow step:
subprocess with each param `shlex`-quoted before substitution (no `shell=True`,
no shell injection — it's not a new code-execution primitive). Each tool lives
as one small file, `harn_env/tools/<name>.json`.

Two ways to create one in Studio's **Tools tab** (under **CUSTOM TOOLS**):

- **Upload a script** — pick a script file, then type in a name, a description,
  and comma-separated param names. harn stores the script alongside the tool and
  builds the command for you as `bash <script> {param} …`.
- **Describe it to the agent** — under *"Describe a new tool to the agent"*, a
  turn-based chat: each **Send** is one message and one agent reply. When the
  agent has a concrete proposal it fills in a **Draft** (name, description,
  params, command); review or refine it over more turns, then click **Save**.

Notes:

- **Available next session, not this one.** The MCP server fetches its tool list
  once, at connect time, so a newly saved or imported tool becomes callable only
  in the agent's **next** session — the UI reminds you of this on every save.
- **Names must be unique** — a custom tool can't reuse a built-in MCP tool name
  or the name of another custom tool (both checks gate every save).
- **Share tools with Export / Import.** **Export** downloads a single portable
  file (plain JSON, or a zip if the tool bundles an uploaded script). **Import**
  loads a file another harn user shared with you; imports are validated (a bundle
  must be a single tool definition, and its name/params are re-checked) before
  the tool lands on disk. **Only import tools from people you trust** —
  validation checks the tool's structure, not its intent, and a valid tool still
  runs the author's shell command on your machine when the agent invokes it.

## Multiple agents, one shared context

Set `agents = ["claude", "codex", …]`; the first installed one runs. Whichever
agent runs shares the **same context** — `AGENTS.md`, the task board,
`state/PROGRESS.md` (append-only history), `state/ANSWERS.md`, and the MCP
server. None of it lives in any single agent's private memory, so the next agent
always knows **what's done and what's planned**. With `loop_aware = true`
(default) that whole picture is injected into every prompt, so you can watch a
task move through its track from inside the agent.

## Human-in-the-loop (Telegram) — set this up, it's the point

**The single most valuable thing you can configure.** harn is built so you can
**walk away from the computer** and still keep the agent on the rails. The agent
does most of the work autonomously, but the moments where a human makes the
difference — a clarifying question on a fuzzy requirement, an "is this what you
meant?" before it goes too far — are exactly the moments that decide whether the
output is right. Telegram puts those moments in your pocket.

Set `HARN_TELEGRAM_BOT_TOKEN` + `HARN_TELEGRAM_CHAT_ID` and harn brings you into
the loop at two points: when the agent **blocks on a question** (`ask_user`) and
when a task is **ready for review**.

- With `wait_for_reply = true` (default), harn posts to your chat and waits.
  - Blocking question → reply with your answer (improves accuracy on ambiguous specs).
  - Review → reply **`approve`** (optionally with notes), or describe the changes.
- **🤖 "Decide for me" button.** Every question comes with an inline button — if
  you don't know or don't care, tap it and the agent picks the best option itself
  (researches best practices, records the decision for review) and keeps going.
  You're never the bottleneck.
- This **survives the computer going to sleep**: Telegram retains the messages,
  so the offset-based long-poll picks up your reply on wake.
- `idle_minutes` re-sends a reminder while waiting; `wait_timeout_minutes`
  (0 = forever) bounds the wait before falling back to the CLI path
  (`harn answer "…"` / `harn review …`).

Without Telegram you're tied to the terminal: harn notifies one-way and you must
answer with CLI commands. With it, a multi-hour backlog can run while you're away,
pinging you only for the few decisions that actually need you.

## Autonomy level — how much to ask vs. decide

```toml
[harn]
autonomy = 0.7   # 0.0 meticulous … 1.0 creative   (env: HARN_AUTONOMY)
```

One dial for the agent's temperament, injected into every planning and execution
turn:

| Level | Behaviour | Use when |
|------:|-----------|----------|
| **0.0–0.3** | Meticulous — clarifies almost everything via `ask_user` | specs are fuzzy / high-stakes / you want control |
| **0.4–0.7** | Balanced — decides routine, reversible things; asks on the big ones | most work (**default 0.7**) |
| **0.8–1.0** | Creative — decides for itself with best practices, rarely asks | you trust the agent / want speed over checkpoints |

Lower autonomy + Telegram = highest accuracy on under-specified work (it asks,
you answer from your phone). Higher autonomy = fewer interruptions. The
`--auto` flag is the extreme: full autonomy, no human at all.

## How the MCP server works

`harn mcp` is the shared brain every agent reaches over MCP. The agent launches
it as a subprocess (via the generated config) — fully local, no open ports:

```
agent (claude/codex/cursor/…) ⇄ stdio ⇄ harn mcp (subprocess)
```

Tools: `list_skills`, `read_skill`, `get_next_task`, `create_task`,
`update_task`, `board`, `ask_user`, `record_decision`, `set_scratchpad`,
`run_tests`, `submit_for_review`, `loop_status` (+ `search` / `find_related` /
`codebase_*` from the code-search servers). Run `harn mcp --http --port 8765` to
serve over `127.0.0.1` instead (same tools; can later sit behind auth/TLS).

**`harn ui` supervision** (`[mcp] ui_supervise`, default `true`): the studio
starts its own `harn mcp --http` child on `[mcp] ui_port` (default `8765`),
restarts it if it dies, and shows its health in the header. This is for
visibility only — it does **not** replace the stdio server your agent already
owns, and agents don't migrate to it. Set `ui_supervise = false` to opt out.

**Hot-reloading custom tools** (`[mcp] tool_reload_seconds`, default `2`, `0`
= disable): every running `harn mcp` process — the agent's stdio one and the
studio's supervised HTTP one — polls `harn_env/tools/` on this interval and
reconciles its tool list against what's on disk. A custom tool you add (or
edit) mid-session becomes callable within a couple of seconds, with no server
restart needed.

## Phases vs. task statuses

- **Loop phase** (`state/STATE.json`): `PLANNING → READY → EXECUTING →
  VERIFYING → UI_VERIFYING → BLOCKED → REVIEW → DONE` — the overall run.
- **Task status** (each `tasks/*.json`): `todo → in_progress → review →
  changes_requested → done` — one task's journey.

## Verify step & token usage

After tests pass, harn runs a **verify turn** (`[loop] verify`, default on): a
dedicated pass that checks the work against the task's acceptance criteria — not
just that tests are green. It fixes small gaps itself, or calls `ask_user` (with
an expanded question) when a human decision is needed, before the task reaches
review. It costs one extra agent turn per task.

When an agent CLI reports token usage (e.g. Claude via `--output-format json`),
harn records the per-task total and cost in the task's Review log and in
`PROGRESS.md`, so you can see what each task cost. Agents that don't expose usage
simply show nothing.

**Run-level budget guard** (`[loop] max_cost_usd`, `[loop] max_tokens`, both
`0` = unlimited, defaults `3.0` / `400000`): harn sums per-turn cost and tokens
as the run progresses, and if either ceiling is crossed the run **stops
immediately and goes to `BLOCKED`** rather than quietly continuing to spend —
a single runaway turn can't burn through the whole budget unnoticed. Change
either value in `harn_env/harn.toml` or from the studio **Settings** tab.
`[loop] turn_timeout_seconds` (default `1800`, `0` = the adapter's own
default) caps how long a single turn's subprocess may run before harn kills
it.

## UI tasks: design-first + browser verification (Playwright MCP)

For user-facing work harn closes the visual loop end to end:

1. **Design before code** (`[loop] design`, default on). During planning the
   agent generates a single-file **HTML mockup** of the final interface
   (`save_design` → `harn_env/design/<task>.html`), you open it in a browser and
   approve it via the normal question flow (chat/Telegram). The approved mockup
   becomes the *visual contract* — it is injected into the executor, oracle, and
   browser-verify prompts.
2. **Test-writing gate** (`[feedback] require_tests`, default on). A turn that
   changes code without touching any test file gets ONE "write tests for this"
   nudge before the work can proceed (once per task — it never loops forever).
3. **Browser verification** (`[browser]`, default off). After tests + verify
   pass on a UI task (one with an approved design or the `ui` skill), harn
   starts your app (`app_cmd`), waits for `app_url`, and runs a dedicated
   **Playwright turn**: the agent drives the live app like a user — walking each
   acceptance criterion, comparing against the approved design, saving
   screenshots to `harn_env/state/screenshots/<task>/` — then harn stops the
   app. `UI: FAIL` loops the task back for rework; the oracle sees the
   screenshots as evidence. Enable it and re-run `harn setup` so the
   `@playwright/mcp` server is added to your agent's MCP config (needs Node.js).
   If the app isn't reachable, the phase is skipped and logged — it never wedges
   the loop.

## Code intelligence — semble + SocratiCode (recommended)

harn integrates two code-search engines so the agent **searches before reading**
— pulling the exact relevant chunks instead of loading whole files. This is the
biggest lever on quality-per-token: published benchmarks show **84% fewer tool
calls and ~60–98% less context** versus grep-and-read. Both are **on by default**
(`[code_search]`), and `harn setup` installs them in one command. Try the full
setup — it's what makes the loop both cheaper and sharper.

| Engine | Responsibility | Why it helps | Needs |
|---|---|---|---|
| **[semble](https://github.com/MinishLab/semble)** | **Semantic chunk retrieval** — "find code that does X" by meaning (vector + BM25, AST chunks) | Finds the right code even when you don't know the name; tiny, in-process, ~ms queries; no infra | bundled with harn (Python) — **zero setup** |
| **[SocratiCode](https://github.com/giancarloerra/SocratiCode)** | **Static dependency graph** — "what breaks if I change X" (blast-radius, call-flow, symbols, 18+ langs) | Precise impact analysis the oracle uses to catch ripple-effects a diff alone hides; lets smaller models handle architectural reasoning | Node.js + **a running Docker** (it auto-pulls & starts its own Qdrant + Ollama — you don't install them) |

**How harn uses them, per phase:**
- **Planning** → `search` for existing patterns, so acceptance criteria match the
  codebase instead of fighting it.
- **Execution** → the agent retrieves chunks instead of reading files (AGENTS.md
  tells it to search first).
- **Oracle** → SocratiCode's `codebase_impact` (precise) or semble's
  `find_related` (semantic), scoped to the diff, to verify the real blast radius.

They have **different jobs and don't overlap** (semantics vs. dependency graph),
so running both is complementary, not redundant. Each does its work **locally**
and sends the model only the relevant slice. Disable either in `harn.toml` if you
want; harn degrades gracefully when one isn't installed.

> `harn setup` installs semble (pip) and checks SocratiCode's prerequisites
> (npx + Docker), warning you if Docker isn't running. Skip with
> `harn setup --no-install`.

## Autonomous mode (`--auto` / `-a`)

```bash
harn run --auto        # or -a
```

No human in the loop. Instead of pausing on each `ask_user`, the agent researches
comprehensive best practices and decides for itself (stating its assumptions),
over a larger iteration budget (`[loop] auto_max_iterations`, default 30). Crucially,
auto mode **never mutates harn_env `.md` files** — no task statuses, no
`PROGRESS.md`/notes, no review log. Code may change; your task track stays
pristine, so it's a safe unattended pass you can inspect afterwards. **Not
recommended for complex tasks** — there's no human checkpoint.

## Driving harn from a chat (interactive)

`harn run` is headless: harn launches the agent for you. If instead you're in a
**chat with an agent** (Cursor, Claude Code) and want the dialogue to stay there,
don't have the agent shell out to `harn run` (that nests a second agent).
Instead the chat agent *is* the loop: it uses the harn MCP tools
(`get_next_task` → work → `run_tests` → `submit_for_review`, `board`) and asks
**you directly in the chat** when unsure. You accept with
`harn review <id> --approve` (or just tell it to move on). This protocol is
spelled out in the generated `AGENTS.md` so any agent follows it.

### Connecting harn MCP to Claude (Claude Code & Claude Desktop)

This is how you get harn's full power — clarifying questions, skill capture,
oracle, socraticode blast-radius — directly inside a Claude chat, without
running `harn run` separately.

#### Step 1 — run `harn setup` in your project (or check it already ran)

```bash
cd /path/to/your-project
harn setup          # scaffolds harn_env/, writes .mcp.json, checks health
```

This generates `.mcp.json` at the project root (and `.cursor/mcp.json` for
Cursor). That file is the only thing Claude needs to find the harn server.

#### How the MCP server starts

You don't start it manually. Claude reads `.mcp.json` and **spawns
`python -m harn mcp` as a stdio subprocess** when the session opens. The
process lives for the duration of the chat and is killed when it closes.
`harn_env/` must exist (created by `harn setup`) and be reachable from the
working directory; the `HARN_ENV_DIR` env-var in `.mcp.json` points to it.

#### Step 2a — Claude Code (desktop app / VS Code / JetBrains extension)

`.mcp.json` is picked up **automatically** when you open the project folder.
Verify it exists and contains harn:

```bash
cat .mcp.json      # should list "harn", "semble", "socraticode"
```

In Claude Code's UI you'll see a 🔌 plug icon or the tool count increase when
MCP servers connect. If harn tools (`ask_user`, `get_next_task`) are not
available in the session, check:
- You opened the folder that contains `.mcp.json` (not a parent folder)
- `harn_env/` exists in that folder
- The Python path in `.mcp.json` matches your environment (`which python3`)

To fix the Python path in `.mcp.json`:

```bash
# Replace the python path with your actual interpreter:
python3 -c "import sys; print(sys.executable)"
# then edit .mcp.json accordingly
```

Or add to `~/.claude/mcp.json` for all projects:

```json
{
  "mcpServers": {
    "harn": {
      "command": "python3",
      "args": ["-m", "harn", "mcp"],
      "cwd": "/path/to/your-project"
    }
  }
}
```

#### Step 2b — Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json`
(create it if it doesn't exist) and add:

```json
{
  "mcpServers": {
    "harn": {
      "command": "python",
      "args": ["-m", "harn.mcp_server"],
      "cwd": "/path/to/your-project"
    }
  }
}
```

Restart Claude Desktop. You'll see a 🔌 icon confirming the server is live.

#### Step 3 — start the optional coordinator (for Telegram escalation)

```bash
harn watch          # runs in background; escalates unanswered questions to Telegram
```

Skip this if you're always at the keyboard and don't need Telegram notifications.

#### Step 4 — paste this to Claude to kick off the workflow

```
You have access to harn MCP tools. Use them for all work in this project:
1. Call `get_next_task` to pick up the next task from the board.
2. During planning, call `ask_user` (with `skill=` for any durable standard)
   before acting on anything ambiguous. Stop after calling it.
3. Write code, run tests with `run_tests`.
4. Call `submit_for_review` — harn runs the oracle automatically.
5. Repeat.
Read `harn_env/AGENTS.md` for the full protocol.
```

#### What you get

| Feature | Without MCP | With harn MCP |
|---|---|---|
| Clarifying questions → saved as skills | ✗ | ✅ `ask_user(skill=…)` |
| Oracle (independent second review) | ✗ | ✅ `submit_for_review` |
| Token savings via socraticode/semble | ✗ | ✅ injected in every planning turn |
| Design-first UI mockups | ✗ | ✅ `save_design` / `read_design` |
| Task board & progress log | ✗ | ✅ `board`, `get_next_task` |
| Telegram escalation | ✗ | ✅ (requires `harn watch`) |

## Where questions go: chat, Telegram, or both (with escalation)

A blocking question (`ask_user`) is routed by `[notify] channel`:

- **`both`** (default): wait for your answer **in the chat** first; if none comes
  within `[notify] chat_grace_minutes` (default 5, or `HARN_CHAT_GRACE_MINUTES`),
  **escalate to Telegram**. Answer in *either* place and both resolve — if you
  answer in the chat after the Telegram card was posted, harn edits the card to
  "answered in chat".
- **`telegram`**: post to Telegram immediately (no chat grace).
- **`chat`**: chat only, never Telegram.

This works **regardless of where the agent runs**. Under `harn run` the loop
does the waiting itself. For a **chat-driven** run (no `harn run`), keep a
coordinator alive next to your chat so escalation still fires:

```bash
harn watch        # after the chat grace, escalates blocked questions to Telegram
```

> Multi-session routing (several agents asking at once, replies matched by
> Telegram reply-to) is the next step — porting the inbox/session store from the
> staidy HIL. Today's coordinator handles one pending question at a time.

## Status (v0.1)

Working: scaffold, config, on-demand skills, loop state machine, feedback runner,
notifications, MCP server (stdio + http, 8 tools), **all five adapters (Claude,
Codex, Cursor, Antigravity, Qwen Code)**, **Telegram human-in-the-loop (answers + review,
with reminders, sleep-safe)**, **per-task review lifecycle**, **multi-agent
shared context / loop-aware prompts**, the loop, and tests.
To implement next: end-to-end `ask_user`/review against a live agent, and the
web board.

## Tests

harn's own tests use pytest (a target project can use any test command):

```bash
pip install -e . pytest
pytest -q
```

---

## harn — на русском

Agent-agnostic «харнесс» для кодинга: ralph-петля, скилы по требованию,
human-in-the-loop и **прозрачный трек ревью по каждой задаче**. Работает с
**Claude Code, Codex, Cursor, Antigravity и Qwen Code** через один общий MCP-сервер.

### Установка и запуск

```bash
pipx install .            # или: pip install -e .   (Python 3.10+)

cd /path/to/your-project
harn setup                # создаёт harn_env/ + AGENTS.md + конфиг MCP

# в harn_env/harn.toml укажите [feedback] test_cmd (команду тестов проекта),
# задачи создаёт агент через create_task (JSON), PRD — в harn_env/prd/*.md

harn run                  # запустить петлю
harn board                # все задачи на их треке
harn status               # текущая фаза; что ждёт вашего ревью

harn review <id> --approve --notes "учесть крайний случай в parse()"
harn review <id> --changes "переименовать модуль в auth/"
```

Секреты — в переменных окружения: `HARN_TELEGRAM_BOT_TOKEN`,
`HARN_TELEGRAM_CHAT_ID`, `HARN_SLACK_WEBHOOK_URL`.

После того как `harn setup` выполнен и MCP подключён к Claude (см. раздел
[Подключение harn MCP к Claude](#подключение-harn-mcp-к-claude-claude-code-и-claude-desktop)),
вставьте этот промпт в чат — и Claude будет работать по полному протоколу harn:

> **Сначала убедись, что MCP-инструменты harn загружены** — в твоём списке
> инструментов должны быть `ask_user`, `answer_question`, `get_next_task`.
> Если их нет — скажи мне, я проверю подключение.
>
> **Используй MCP-инструменты harn для всей работы. Никогда не задавай
> вопросы текстом в чате** — всегда через `ask_user(question, skill=…)`,
> чтобы ответы автоматически сохранялись в скиллы.
> 1. Вызови `get_next_task` — возьми следующую задачу с доски.
> 2. При планировании вызывай `ask_user(question, skill=…)` перед любым
>    неоднозначным решением. После вызова стоп — я отвечу в чате, потом ты
>    вызываешь `answer_question(answer=…)` и продолжаешь.
> 3. Пиши код. Запускай `run_tests`. Исправляй до зелёного.
> 4. Вызови `submit_for_review` — harn автоматически запустит оракула.
> 5. Повторяй, пока `board` не покажет все задачи выполненными.
> Прочитай `harn_env/AGENTS.md` — там полный протокол.

### Идея

harn разделяет **мозг** (переносимый) и **руки** (свои у каждого агента). Мозг —
один MCP-сервер: задачи, скилы, `ask_user`, фидбек-петля, уведомления — работают
одинаково везде. Руки — тонкий адаптер на агента (`claude -p`, `codex exec`,
`cursor-agent -p`, `antigravity exec`, `qwen -p`); реализованы все пять. Добавить
нового агента — это маленький адаптер с одним `run_turn()`. Петлёй управляет
сам harn, поэтому планирование, сигнал тестов, ревью-гейт и «остановись и
спроси» ведут себя одинаково независимо от агента.

### Что создаёт `harn setup`

Полная структура `harn_env/` — в каталоге **[harn_example/](./harn_example/)**.
Весь контекст харнесса хранится в `harn_env/`: `harn.toml`, `skills/`, `prd/`,
`tasks/` (по файлу на задачу), `state/` (`STATE.json`, `PROGRESS.md`,
`ANSWERS.md`, `BLOCKED.md`) и `mcp_snippets.md`. В корне проекта: `AGENTS.md`,
`.mcp.json`, `.cursor/mcp.json`.

### Задачи (JSON) и PRD (Markdown)

**Задачи — JSON-файлы** в `harn_env/tasks/`, имя файла = id задачи:
- с трекером → ключ: `AUTH-42.json` (поля `epic`/`user_story` ведут вверх);
- без трекера → harn авто-нумерует `PRJ-001.json`, `PRJ-002.json`… из `[harn] project`.

Задача ссылается на один или несколько PRD массивом `prds` (может охватывать
несколько), и несёт свой структурированный `review_log`, `decisions`,
`scratchpad`. Вручную JSON не пишут — агент вызывает MCP-инструменты
`create_task` / `update_task`.

**PRD — Markdown** в `harn_env/prd/<slug>.md`: YAML-frontmatter (`id`, `status`,
`priority`) + секции (`## Проблема`/`## Problem`, `## Цель`, `## Содержание`,
`## Критерии приёмки` — русские заголовки распознаются). Пишут продакт/аналитик,
агент нормализует в начале цикла.

### Трек задачи

```
todo → in_progress → review ⇄ changes_requested → done
```

Падающие тесты возвращают задачу в `in_progress`. После прохождения тестов
задача уходит на `review` — harn просит вас принять или прокомментировать.
Комментарий → `changes_requested` (агент переделывает). Принятие → `done` с
секцией **«Notes for future agents»**. История ревью дописывается в сам файл
задачи, поэтому контекст путешествует вместе с ней.

### Создание и запуск задач прямо с доски

Чтобы завести задачу, не нужна сессия агента:

- **«＋ New task»** на вкладке Board создаёт задачу прямо из Studio —
  обязателен только заголовок; описание, флоу и приоритет необязательны и по
  умолчанию пустые/дефолтный флоу проекта/`10`.
- **Статус** задачи можно поменять прямо в панели деталей — выпадающим
  списком рядом с id задачи (это обычная смена статуса — она не трогает
  `review_log` и не считается accept/request-changes).
- **Перед переводом в `in_progress` обязательно нужно выбрать флоу.**
  Селектор флоу всегда показывает реальный, явный выбор — «default workflow»
  тоже считается таким выбором, так что повторное подтверждение дефолта
  засчитывается. Если флоу ещё не выбирали, перевод статуса в `in_progress`
  отклоняется с понятной ошибкой, а статус остаётся прежним.
- Перевод задачи в `in_progress` **сразу запускает её прогон** в фоне — по
  тому же правилу «один прогон одновременно», что и у кнопки **▶ Launch**:
  если уже активен другой прогон, запуск отклоняется, а смена статуса
  откатывается — задача никогда не остаётся «застрявшей» в `in_progress` без
  реально работающего прогона.
- **«Open flow ▶»** в панели деталей задачи переключает на вкладку Flow с уже
  выбранной этой задачей — можно смотреть выполнение вживую.
- Просмотр плана ещё не начатой задачи (или превью промпта шага) больше не
  замораживает её — реальный план задачи фиксируется только когда что-то его
  реально выполняет (полный прогон или одиночный **Run** шага), поэтому смена
  флоу до этого момента по-настоящему на что-то влияет.

### Шаги воркфлоу: agent/command, on-fail, parallel

`harn_env/WORKFLOW.md` — план, по которому идёт каждая задача (правьте руками
или через визуальный Studio-канвас `harn ui`). У шага есть необязательные
строки:
- **`Type: command`** — шаг-команда вместо хода агента (по умолчанию
  `Type: agent`), с **`Command: <shell>`** и опциональным
  **`On fail: <step title>`** (при ошибке один раз запускает целевой шаг как
  агента-починщика, затем команда повторяется).
- **`Parallel: <group>`** — шаги с одним `group` id выполняются одновременно.
  В Studio для этого достаточно перетащить блоки на один Y-уровень канваса —
  поле `Parallel` и есть источник истины, позиция на канвасе лишь жест
  редактирования. Каждый шаг группы выполняется в своём изолированном git
  worktree от общего чекпоинта; после завершения все патчи по очереди
  сливаются в ваше рабочее дерево (конфликт в одном файле решает один ход
  агента-мерджера) — коммиты harn по-прежнему не создаёт.
- **`On fail:` внутри `Parallel`-группы не поддерживается** — совмещать
  retry/handler-переходы с параллелизмом решили не делать. Если оба поля
  заданы одновременно, пишется событие `config_error`, а `On fail`
  игнорируется для этого шага (ошибка просто фиксируется, без хендлера).

### Наблюдаемость шагов: уровни скилов/тулов, бейджи использования, превью контекста

- **`Skills (required: a; recommended: b, c)`** и **`Tools (required: x;
  recommended: y)`** — `required`-элементы всегда загружаются и проверяются;
  `recommended` — показываются агенту, но не загружаются принудительно.
  Старые формы по-прежнему работают: `Skills (required: a)` (без секции
  recommended) и `Tools: x, y` (сахар для «всё recommended, ничего
  required»).
- После выполнения шага инспектор Studio показывает **зелёный/жёлтый/красный
  бейдж** на каждом чипе скила/тула: зелёный — использован, жёлтый —
  рекомендован, но не использован, красный — обязателен, но не использован.
- **Обязательный и неиспользованный** скил/тул запускает ровно один
  автоматический повтор того же шага с напоминанием в промпте; если и на
  повторе он не использован — harn сам блокирует прогон (как любой другой
  блок) для решения человеком. Это применяется только к последовательным
  шагам — шаг внутри параллельной волны по-прежнему фиксирует и показывает
  использование, но никогда не повторяется и не блокируется этой проверкой.
- Кнопка **«View full context»** в инспекторе шага показывает точный промпт,
  который шаг получит (или получил); **«Copy to file»** экспортирует его в
  текстовый файл в `harn_env/state/context_exports/`.
- Кнопка доски **«⏸ Pause»** (переименованный Stop, та же остановка) — уже
  выполненные шаги остаются выполненными; отредактируйте план и нажмите
  **▶ Resume**, чтобы продолжить только оставшиеся шаги.
- Когда прогон **BLOCKED**, доска показывает вопрос прямо там, с полем для
  ответа, вместо `harn answer` из терминала (используется тот же
  `loop.answer()`, что и CLI). Без изменений: если никто не ответит вовремя
  (в чате, Studio или CLI), harn по-прежнему эскалирует тот же вопрос в
  Telegram после `chat_grace_minutes`.

### Свои инструменты (custom tools) — расширьте набор агента

Помимо встроенных MCP-тулов вы можете добавлять **свои инструменты**, которые
агент сможет вызывать. Custom tool — это **имя + описание + список параметров +
шаблон shell-команды** с плейсхолдерами `{param}` (например, `command` вида
`bash lint.sh {path}`). Он выполняется ровно как шаг `Type: command`:
subprocess, каждый параметр `shlex`-квотируется перед подстановкой (без
`shell=True`, без shell-инъекций — это не новый примитив исполнения кода). Каждый
инструмент — один маленький файл `harn_env/tools/<name>.json`.

Два способа создать его во вкладке **Tools** (раздел **CUSTOM TOOLS**):

- **Загрузить скрипт** — выберите файл скрипта, затем впишите имя, описание и
  параметры через запятую. harn сохранит скрипт рядом с инструментом и сам
  соберёт команду вида `bash <script> {param} …`.
- **Описать инструмент агенту** — в блоке *«Describe a new tool to the agent»*,
  пошаговый чат: каждый **Send** — одно сообщение и один ответ агента. Когда у
  агента есть конкретное предложение, он заполняет **Draft** (имя, описание,
  параметры, команда); уточните за несколько ходов и нажмите **Save**.

Важно:

- **Доступен со следующей сессии, не с текущей.** MCP-сервер запрашивает список
  тулов один раз, при подключении, поэтому только что сохранённый или
  импортированный инструмент станет доступен агенту лишь в его **следующей**
  сессии — UI напоминает об этом при каждом сохранении.
- **Имена уникальны** — custom tool не может совпасть по имени со встроенным
  MCP-тулом или с другим custom tool (обе проверки на каждом сохранении).
- **Обмен через Export / Import.** **Export** скачивает один переносимый файл
  (JSON или zip, если у инструмента есть загруженный скрипт). **Import**
  загружает файл, которым поделился другой пользователь harn; импорт проходит
  валидацию (в бандле должно быть ровно одно определение инструмента, имя и
  параметры перепроверяются) прежде чем инструмент попадёт на диск.
  **Импортируйте инструменты только от тех, кому доверяете** — валидация
  проверяет структуру инструмента, но не его намерение, и корректный инструмент
  всё равно выполнит shell-команду автора на вашей машине, когда агент его вызовет.

### Несколько агентов, один контекст

`agents = ["claude", "codex", …]` — запустится первый установленный. Любой агент
работает с общим контекстом (`AGENTS.md`, доска задач, `state/PROGRESS.md`,
`state/ANSWERS.md`, MCP), поэтому следующий агент всегда знает, что сделано и что
запланировано. При `loop_aware = true` (по умолчанию) всё это подставляется в
каждый промпт — ход выполнения виден прямо изнутри агента.

### Human-in-the-loop в Telegram — обязательно настройте

**Самое ценное, что стоит включить.** Смысл harn — **отойти от компьютера**, но
не пропустить те несколько решений, которые определяют правильность результата.
Агент делает основную работу сам, а уточнения по размытым требованиям прилетают
вам в карман. Настройте `HARN_TELEGRAM_BOT_TOKEN` + `HARN_TELEGRAM_CHAT_ID`.

При `wait_for_reply = true` (по умолчанию) harn пишет вам в Telegram и ждёт —
и на блокирующий вопрос (`ask_user`), и на ревью (`approve <заметки>` или
описание правок). Это **переживает засыпание компьютера**: long-poll по offset
подхватывает ответ после пробуждения. `idle_minutes` — частота напоминаний,
`wait_timeout_minutes` (0 = бесконечно) — таймаут до отката на CLI.

- **🤖 Кнопка «Decide for me».** К каждому вопросу прикреплена inline-кнопка: если
  не знаете или не важно — жмёте, и агент сам выбирает лучший вариант (по best
  practices, фиксируя решение для ревью) и продолжает. Вы никогда не узкое место.

### Уровень самостоятельности (`[harn] autonomy`)

Один регулятор характера агента, `0.0`–`1.0` (env `HARN_AUTONOMY`), по умолчанию
**0.7**. Подставляется в каждый ход:
- **0.0–0.3** — дотошный: уточняет почти всё через `ask_user` (жёсткий контроль).
- **0.4–0.7** — сбалансированный: решает рутинное/обратимое сам, спрашивает по
  важному.
- **0.8–1.0** — творческий: решает сам по best practices, спрашивает редко.

Низкая самостоятельность + Telegram = максимум точности на размытых задачах
(агент спросит — вы ответите с телефона). Высокая = меньше отвлечений.

### Code intelligence — semble + SocratiCode (рекомендуется)

Два движка поиска по коду: агент **ищет, а не читает целиком** — берёт только
нужные фрагменты. Это главный рычаг качества-на-токен (бенчмарки: на 84% меньше
вызовов инструментов, на 60–98% меньше контекста). Оба включены по умолчанию,
`harn setup` ставит их одной командой.
- **semble** — *семантический поиск* («найди код, который делает X»). В процессе,
  без инфраструктуры, идёт с harn. Отвечает за «что похоже».
- **SocratiCode** — *граф зависимостей* («что сломается, если поменять X»).
  Точный blast-radius для оракула. Нужен Node.js + запущенный Docker (Qdrant +
  Ollama он поднимает сам, ставить не надо).

Разные задачи, не дублируют друг друга. Каждый работает **локально** и шлёт в
модель только релевантный срез.

**Куда идёт вопрос — `[notify] channel`:**
- **`both`** (по умолчанию): сначала ждём ответа **в чате**; если за
  `chat_grace_minutes` (по умолчанию 5; env `HARN_CHAT_GRACE_MINUTES`) ответа нет
  — **эскалация в Telegram**. Ответить можно где угодно: ответ в чате после
  отправки в Telegram отредактирует карточку на «отвечено в чате».
- **`telegram`**: сразу в Telegram (без грации чата).
- **`chat`**: только чат, без Telegram.

Это работает **независимо от того, где запущен агент**. В `harn run` ждёт сама
петля. Для чат-режима (без `harn run`) держите рядом координатор —
`harn watch` — он и эскалирует вопрос в Telegram после грации.

### UI-задачи: дизайн до кода + проверка в браузере (Playwright MCP)

Для пользовательских интерфейсов harn замыкает визуальный цикл:
1. **Дизайн до кода** (`[loop] design`, вкл. по умолчанию): на этапе
   планирования агент генерирует **HTML-мокап** интерфейса
   (`harn_env/design/<task>.html`), вы открываете его в браузере и утверждаете
   через обычный канал вопросов. Утверждённый мокап — визуальный контракт для
   исполнителя, оракула и браузерной проверки.
2. **Гейт на тесты** (`[feedback] require_tests`, вкл. по умолчанию): ход,
   меняющий код без тестов, получает один возврат «напиши тесты» (не чаще
   одного раза на задачу).
3. **Проверка в браузере** (`[browser]`, выкл. по умолчанию): после тестов и
   verify на UI-задаче harn сам запускает приложение (`app_cmd`), ждёт
   `app_url` и даёт агенту отдельный ход через **Playwright MCP**: пройти
   каждый критерий приёмки как живой пользователь, сравнить с мокапом, сложить
   скриншоты в `harn_env/state/screenshots/<task>/`. `UI: FAIL` возвращает
   задачу в доработку; скриншоты видит оракул. Включите и перезапустите
   `harn setup` — он добавит `@playwright/mcp` в MCP-конфиг агента (нужен
   Node.js). Недоступное приложение фазу не вешает — она пропускается с записью
   в лог.

### Автономный режим (`harn run --auto` / `-a`)

Без человека: вместо ожидания ответа на каждый `ask_user` агент сам исследует
лучшие практики и принимает решение (фиксируя допущения), за больший бюджет
итераций (`auto_max_iterations`, по умолчанию 30). Главное — auto **никогда не
меняет .md-файлы** в `harn_env/` (статусы задач, `PROGRESS`, заметки): код
поменяться может, а трек задач остаётся нетронутым. **Не для сложных задач** —
человеческого чекпоинта нет.

### Диалог в чате с агентом

`harn run` — headless (harn сам запускает агента). Если же вы **в чате с агентом**
(Cursor, Claude Code) и хотите, чтобы диалог шёл там, не заставляйте агента
вызывать `harn run` (это вложит второго агента). Вместо этого агент в чате сам
*становится* петлёй: использует MCP-инструменты (`get_next_task` → работа →
`run_tests` → `submit_for_review`, `board`) и задаёт вопросы **прямо вам в чате**.
Приёмка — `harn review <id> --approve`. Протокол описан в сгенерированном
`AGENTS.md`.

### Подключение harn MCP к Claude (Claude Code и Claude Desktop)

Так вы получаете всю мощь harn — уточняющие вопросы, сохранение стандартов в
скиллы, оракул, socraticode — прямо в чате с Claude, без отдельного запуска
`harn run`.

#### Шаг 1 — запустите `harn setup` в проекте

```bash
cd /path/to/your-project
harn setup          # создаёт harn_env/, пишет .mcp.json, проверяет сервер
```

В корне проекта появится `.mcp.json` — это всё, что нужно Claude для
подключения.

#### Как запускается MCP-сервер

Вручную его запускать не нужно. Claude читает `.mcp.json` и **сам запускает
`python -m harn mcp` как подпроцесс** при открытии сессии. Процесс живёт
ровно пока открыт чат — потом завершается. Папка `harn_env/` должна
существовать (её создаёт `harn setup`); путь к ней прописан в переменной
`HARN_ENV_DIR` внутри `.mcp.json`.

#### Шаг 2a — Claude Code (CLI / расширение VS Code / JetBrains)

`.mcp.json` подхватывается **автоматически** при открытии папки проекта.
Проверьте что файл существует и содержит harn:

```bash
cat .mcp.json      # должны быть "harn", "semble", "socraticode"
```

В интерфейсе Claude Code появится значок 🔌 или увеличится счётчик инструментов.
Если инструменты harn (`ask_user`, `get_next_task`) недоступны — проверьте:
- Открыта именно папка с `.mcp.json`, а не родительская
- `harn_env/` существует в этой папке
- Python-путь в `.mcp.json` совпадает с вашим окружением:

```bash
python3 -c "import sys; print(sys.executable)"
# вставьте результат в .mcp.json как "command"
```

Или добавьте в `~/.claude/mcp.json` глобально (для всех проектов):

```json
{
  "mcpServers": {
    "harn": {
      "command": "python3",
      "args": ["-m", "harn", "mcp"],
      "cwd": "/path/to/your-project"
    }
  }
}
```

#### Шаг 2b — Claude Desktop

Откройте `~/Library/Application Support/Claude/claude_desktop_config.json`
(создайте, если нет) и добавьте:

```json
{
  "mcpServers": {
    "harn": {
      "command": "python",
      "args": ["-m", "harn.mcp_server"],
      "cwd": "/path/to/your-project"
    }
  }
}
```

Перезапустите Claude Desktop — появится значок 🔌.

#### Шаг 3 — запустите координатор (опционально, для Telegram)

```bash
harn watch          # эскалирует неотвеченные вопросы в Telegram
```

Можно пропустить, если вы всегда рядом и уведомления не нужны.

#### Шаг 4 — вставьте в чат с Claude, чтобы начать работу

```
У тебя есть MCP-инструменты harn. Используй их для всей работы в этом проекте:
1. Вызови `get_next_task` — возьми следующую задачу с доски.
2. При планировании вызывай `ask_user` (с `skill=` для любого стандарта)
   перед любым неоднозначным решением. После вызова остановись.
3. Пиши код, запускай тесты через `run_tests`.
4. Вызови `submit_for_review` — harn автоматически запустит оракула.
5. Повторяй.
Прочитай `harn_env/AGENTS.md` — там полный протокол.
```

#### Что вы получаете

| Возможность | Без MCP | С harn MCP |
|---|---|---|
| Уточняющие вопросы → сохраняются в скиллы | ✗ | ✅ `ask_user(skill=…)` |
| Оракул (независимая проверка) | ✗ | ✅ `submit_for_review` |
| Экономия токенов через socraticode/semble | ✗ | ✅ инжектируется при планировании |
| Дизайн UI до кода | ✗ | ✅ `save_design` / `read_design` |
| Доска задач и лог прогресса | ✗ | ✅ `board`, `get_next_task` |
| Эскалация в Telegram | ✗ | ✅ (требует `harn watch`) |
