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

## Quickstart

```bash
cd /path/to/your-project
harn setup                # scaffolds harn_env/ + AGENTS.md + per-agent MCP config
                          # also installs enabled code-search backends (semble;
                          # SocratiCode prereqs). Skip with: harn setup --no-install

# edit harn_env/harn.toml -> [feedback] test_cmd = "pytest -q"   (your tests)
# add tasks in harn_env/tasks/*.json, PRDs in harn_env/prd/*.md

harn run                  # run the loop with the configured agent(s)
harn board                # see every task on its track
harn status               # current phase; what's awaiting your review
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

[feedback]
test_cmd = "pytest -q"              # your project's tests (any language)

[loop]
max_iterations = 10
loop_aware = true                   # agent sees the board + progress + lifecycle
verify = true                       # extra turn: check work vs acceptance criteria
auto = false                        # autonomous (also `harn run --auto`); see below
auto_max_iterations = 30            # bigger budget for unattended runs

[notify]
idle_minutes = 30                   # reminder cadence while waiting on you
wait_for_reply = true               # answer & review right in Telegram
wait_timeout_minutes = 0            # 0 = wait forever; else fall back to CLI
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
│  ├─ prd/<project>-prd<NNN>-<slug>.md      # product requirement docs
│  ├─ tasks/<project>-prd<NNN>-task<NNN>-<slug>.md  # backlog — one file per task
│  ├─ state/                   # runtime state, written by the loop
│  │   ├─ STATE.json          # loop phase, current task, iterations
│  │   ├─ PROGRESS.md         # append-only history every agent reads on pickup
│  │   ├─ ANSWERS.md          # your answers to blocking questions
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

## Task & PRD naming (project + PRD prefix)

Every task belongs to a PRD, and every PRD to a project, so a task's filename
carries that whole lineage:

```
harn_env/prd/prj001-prd001-checkout.md
harn_env/tasks/prj001-prd001-task001-add-cart.md
               └ project ┘└ prd  ┘└ task  ┘└── human slug ──┘
```

The leading `prj…-prd…-task…` is the stable **code**; the trailing slug is for
humans. Set the project code once in `[harn] project`. Reference a task by its
full name, its code (`prj001-prd001-task001`), or the bare `task001`. Plain
names without the code still work (older backlogs keep running) — they just have
no project/PRD grouping.

## The task track (transparent, per task)

Each task is one Markdown file whose `status:` line walks a human-visible track.
The review thread and acceptance notes are appended to the task's own file, so
the full history travels with it to any future agent.

```
todo → in_progress → review ⇄ changes_requested → done
```

1. **todo** — you added it.
2. **in_progress** — an agent is working it (failing tests loop it back here).
3. **review** — the agent finished; harn asks **you** to accept or comment.
4. **changes_requested** — you left a comment; the agent reworks it.
5. **done** — you accepted it; your **notes for future agents** are saved into
   the task file.

See [`harn_example/tasks/prj001-prd001-task002-jwt-auth.md`](./harn_example/tasks/prj001-prd001-task002-jwt-auth.md)
for a `done` task with its full review log and carried-forward notes.

## Multiple agents, one shared context

Set `agents = ["claude", "codex", …]`; the first installed one runs. Whichever
agent runs shares the **same context** — `AGENTS.md`, the task board,
`state/PROGRESS.md` (append-only history), `state/ANSWERS.md`, and the MCP
server. None of it lives in any single agent's private memory, so the next agent
always knows **what's done and what's planned**. With `loop_aware = true`
(default) that whole picture is injected into every prompt, so you can watch a
task move through its track from inside the agent.

## Human-in-the-loop (Telegram)

Set the Telegram env vars and harn brings you into the loop at two moments:
when the agent **blocks on a question** (`ask_user`) and when a task is **ready
for review**.

- With `wait_for_reply = true` (default), harn posts to your chat and waits.
  - Blocking question → reply with your answer.
  - Review → reply **`approve`** (optionally with notes) to accept, or just
    describe the changes you want.
- This **survives the computer going to sleep**: Telegram retains the messages,
  so the offset-based long-poll picks up your reply on wake.
- `idle_minutes` re-sends a reminder while waiting; `wait_timeout_minutes`
  (0 = forever) bounds the wait before falling back to the CLI path
  (`harn answer "…"` / `harn review …`).

Without Telegram, harn just notifies (Telegram/Slack one-way) and waits for the
CLI commands.

## How the MCP server works

`harn mcp` is the shared brain every agent reaches over MCP. The agent launches
it as a subprocess (via the generated config) — fully local, no open ports:

```
agent (claude/codex/cursor/…) ⇄ stdio ⇄ harn mcp (subprocess)
```

Tools: `list_skills`, `read_skill`, `get_next_task`, `board`, `ask_user`,
`run_tests`, `submit_for_review`, `loop_status`. Run `harn mcp --http --port 8765` to
serve over `127.0.0.1` instead (same tools; can later sit behind auth/TLS).

## Phases vs. task statuses

- **Loop phase** (`state/STATE.json`): `PLANNING → READY → EXECUTING →
  VERIFYING → BLOCKED → REVIEW → DONE` — the overall run.
- **Task status** (each `tasks/*.md`): `todo → in_progress → review →
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
# добавьте задачи в harn_env/tasks/*.md

harn run                  # запустить петлю
harn board                # все задачи на их треке
harn status               # текущая фаза; что ждёт вашего ревью

harn review <id> --approve --notes "учесть крайний случай в parse()"
harn review <id> --changes "переименовать модуль в auth/"
```

Секреты — в переменных окружения: `HARN_TELEGRAM_BOT_TOKEN`,
`HARN_TELEGRAM_CHAT_ID`, `HARN_SLACK_WEBHOOK_URL`.

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

### Имена задач и PRD (префикс проекта + PRD)

Каждая задача принадлежит PRD, а PRD — проекту, поэтому имя файла несёт всю
цепочку:

```
harn_env/prd/prj001-prd001-checkout.md
harn_env/tasks/prj001-prd001-task001-add-cart.md
```

Префикс `prj…-prd…-task…` — устойчивый **код**, хвост-слаг — для людей. Код
проекта задаётся раз в `[harn] project`. Ссылаться на задачу можно полным
именем, кодом (`prj001-prd001-task001`) или коротким `task001`. Простые имена
без кода тоже работают (старые бэклоги продолжают жить) — просто без группировки
по проекту/PRD.

### Трек задачи

```
todo → in_progress → review ⇄ changes_requested → done
```

Падающие тесты возвращают задачу в `in_progress`. После прохождения тестов
задача уходит на `review` — harn просит вас принять или прокомментировать.
Комментарий → `changes_requested` (агент переделывает). Принятие → `done` с
секцией **«Notes for future agents»**. История ревью дописывается в сам файл
задачи, поэтому контекст путешествует вместе с ней.

### Несколько агентов, один контекст

`agents = ["claude", "codex", …]` — запустится первый установленный. Любой агент
работает с общим контекстом (`AGENTS.md`, доска задач, `state/PROGRESS.md`,
`state/ANSWERS.md`, MCP), поэтому следующий агент всегда знает, что сделано и что
запланировано. При `loop_aware = true` (по умолчанию) всё это подставляется в
каждый промпт — ход выполнения виден прямо изнутри агента.

### Human-in-the-loop в Telegram

При `wait_for_reply = true` (по умолчанию) harn пишет вам в Telegram и ждёт —
и на блокирующий вопрос (`ask_user`), и на ревью (`approve <заметки>` или
описание правок). Это **переживает засыпание компьютера**: long-poll по offset
подхватывает ответ после пробуждения. `idle_minutes` — частота напоминаний,
`wait_timeout_minutes` (0 = бесконечно) — таймаут до отката на CLI.

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
