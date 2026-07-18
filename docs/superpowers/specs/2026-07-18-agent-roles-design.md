# Agent Roles Design

## Goal

An "Agent" is a named role (a virtual employee: Analyst, Developer) that services one board status: it takes a task, runs it to completion through harn's Ralph loop (attempts + feedback + optional independent oracle re-check), records the result into the task file, and transitions the task to its configured next status. This spec covers the role entity and running it; triggers (Telegram/API/status-watch) are the companion triggers spec.

## Depends on

- Custom board statuses spec (`2026-07-18-custom-board-statuses-design.md`) — roles are keyed to statuses.
- Task context/markdown storage (spec B, implemented) — results and context land in `<task>.md`.

## The role entity

`harn_env/agents/<name>.md` — frontmatter + prose, same pattern as skills and task files:

```markdown
---
name: analyst
command: analyst        # telegram slash command (triggers spec)
status: analyzing       # board status this role services
trigger: manual         # auto | manual (default manual; triggers spec)
next_status: analyzed   # transition on success; empty = no transition
workflow: analyst-flow  # workflow preset its runs execute
oracle: true            # independent re-check gates the transition (default true)
secrets: [SSH_HOST, SSH_USER]   # required env var NAMES from the project secrets file
isolation: main         # main | worktree
agent: claude           # CLI adapter override (optional)
model: sonnet           # model override (optional)
---
## Role
You are a support analyst. ... (persona + instructions, injected into
every run this role performs)
```

Discovery mirrors `skills.py`/`tools.py`: the directory of files IS the source of truth; `agents.discover(env_dir)` parses frontmatter; invalid definitions are skipped with a logged reason, never crash.

## Secrets

One project-level `harn_env/secrets.env` (`KEY=value` lines, gitignored by scaffold, chmod 600 on write). A role declares the NAMES it needs. At launch:

- Missing declared keys → the run refuses to start with a clear message (fail fast, not mid-task).
- Values are injected **only into the spawned run's process environment**. The prompt lists available variable names so the model knows what its tools can use; values never enter the prompt, the task file, transcripts, or Telegram messages. Custom tools (e.g. an `ssh_exec` script) read them from `os.environ`.

## Running a role

`harn run --task X --as analyst` (CLI) and `runner.launch(..., as_agent="analyst")` (Studio/dispatcher):

1. Resolve the role; verify the task's status matches `status:` (warn + proceed on explicit manual run — the human is the authority; auto triggers never mismatch).
2. Claim the task (existing claim mechanism), set its workflow to the role's preset for this run.
3. `isolation: worktree` → run in a fresh git worktree and merge the resulting patch back on success — reusing the parallel-wave worktree + patch machinery verbatim. `main` (default) → run in place.
4. The run is a normal harn Ralph-loop execution of the role's workflow: same attempts budget, feedback (tests), context capture into `<task>.md` — plus the role's `## Role` body prepended into every step prompt (after AGENTS.md, before the skills index).
5. On workflow completion: write the final summary into the task's `## Result`; if `oracle: true`, run the existing oracle turn against the task's acceptance criteria — a failed verdict keeps the task in the current status with the verdict recorded (the Ralph loop's rework path picks it up on the next attempt/launch); a passing verdict (or `oracle: false`) transitions the task to `next_status`.
6. Transition fires the external-tracker push seam (below) and the notification callback (triggers spec).

## External tracker seam

`harn/trackers.py`: a minimal adapter interface (`fetch_task(ref)`, `push_status(task)`, `push_result(task)`) with a single null implementation registered. The transition and result-write points in step 5-6 call through it (no-ops today). Real GitLab/Linear/ClickUp providers are separate future specs, one file each, mirroring how `adapters/` wraps agent CLIs.

## Out of scope (this spec)

- Triggers: status-watch auto-pickup, Telegram commands, API endpoint (companion spec; `trigger:` field is stored but only manual CLI/Studio launch works after this spec).
- Studio UI for editing role files (they're markdown; the Skills tab pattern can be extended later).
- Multiple concurrent role runs per project (single-runner constraint stays; the dispatcher queues).

## Verification

- Unit: role discovery (valid/invalid/missing fields), secrets fail-fast on missing keys, env injection reaches the spawned process while prompt contains names only (assert value absent from built prompt).
- Unit: successful run transitions to `next_status`; oracle-fail keeps status and records verdict; `next_status` empty → no transition.
- Unit: worktree isolation merges the patch; main-mode runs in place.
- Integration-style: a fake-adapter end-to-end run of a two-role chain on a custom pipeline (statuses spec) asserting the task walks `analyzing → analyzed` with Result written.
