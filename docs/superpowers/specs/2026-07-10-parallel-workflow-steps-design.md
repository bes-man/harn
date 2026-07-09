# Parallel workflow steps (Phase 3)

## Problem

Phases 1–2 made `harn run` walk a task's own workflow plan — arbitrary steps,
each an agent turn or a shell command, executed strictly one at a time in file
order. Independent work (e.g. "Backend API" and "Frontend UI") that touches
disjoint files still runs sequentially, wasting wall-clock.

Users want to mark a set of steps as parallel — expressed by placing their
blocks at the same horizontal level on the studio canvas — and have `harn run`
execute them concurrently, provided they:
- don't corrupt each other's file edits,
- all contribute to the SAME single project/task context (skills, decisions,
  scratchpad) without write conflicts,
- can each be rolled back independently of the others.

## Non-goals (deferred)

- `On fail:` transitions INSIDE a parallel wave (retry/handler jumps combined
  with concurrency is combinatorial). A parallel step declaring `On fail:` is a
  config error, logged and ignored — same treatment as an `on_fail` pointing at
  a command step (Phase 2).
- Nested parallelism (a wave inside a wave).
- Cross-wave data dependencies beyond "the whole prior wave finished."

## Design

### Data model — `Parallel: <group>` per step (WORKFLOW.md)

A new optional per-step line, same convention as `Type:`/`On fail:`:

```
## 3. Backend API
Parallel: wave-1
...
## 4. Frontend UI
Parallel: wave-1
```

- Parsed/composed by `harn/workflow.py` into a node field `parallel` (`str`,
  `""` default). Round-trips like every other field.
- The `parallel` field is AUTHORITATIVE for execution and is what other agents
  (Codex/Cursor reading WORKFLOW.md) see — the canvas Y-position is only the
  editing GESTURE, never the source of truth (avoids the "a stray drag changes
  execution semantics" fragility).

### Gesture → field bridge (studio)

On drag-end, `deriveParallelGroups()` buckets enabled step nodes into
horizontal bands (Y within a threshold, e.g. ±30px of each other) that are
also CONTIGUOUS in the ordered step list. A band with ≥2 nodes gets a shared
group id assigned to each member's `parallel` field (reuse the band's existing
group id if it already had one; else mint `wave-<6hex>`). A band with one node
gets `parallel=""`. This updates the in-memory node fields, marks the flow
dirty, and re-renders — so the lane appears immediately on aligning, and Save
persists exactly what's shown. (Save does NOT re-derive from Y — it persists
the already-computed fields, so the field and the visual never disagree.)

### Visual — the "lane" (canvas)

For each group with ≥2 members, `renderFlow()` draws BEHIND the member nodes a
translucent rounded container (a "lane") spanning them horizontally, with a
header chip `∥ parallel · <group>` at its top-left. Edges from the step BEFORE
the wave fan out to every member; edges from every member re-converge into the
step AFTER the wave. The lane is a non-interactive backdrop (z-index below
nodes, `pointer-events:none`) so dragging/selecting members is unaffected.
Members keep their normal per-step inspector, plus a small "part of parallel
group <group> — runs concurrently with N other step(s)" note and a "Make
sequential" button that clears `parallel` on all members of the group.

### Execution — the wave (harn/loop.py)

`run()`'s step walk groups CONSECUTIVE enabled steps sharing a non-empty
`parallel` id into a wave. A wave of one degrades to a normal single-step run
(no worktrees). For a wave of N≥2:

1. **Checkpoint the current (possibly dirty) working tree** with the existing
   `gitutil.checkpoint(project_root, task.id, wave_id)` — `git stash create`
   captures staged+unstaged+untracked into a dangling commit. This is the
   wave's base state AND its rollback target. Call it `base_ref`.
2. **One git worktree per step** off `base_ref`:
   `git worktree add --detach <tmp>/harn-wave/<wave_id>/<step_id> <base_ref>`.
   Each worktree is the exact base-state tree, isolated on disk.
3. **Provider-agnostic wiring into each worktree** (this is what makes it work
   for ANY agent CLI — see "Agent-agnostic" below): copy the main project
   root's connector files (`.mcp.json`, `.cursor/mcp.json`, `AGENTS.md`,
   `CLAUDE.md` — whichever exist) into the worktree root, and in each copied
   MCP config rewrite `env.HARN_ENV_DIR` from the relative `"harn_env"` to the
   ABSOLUTE path of the MAIN project's `harn_env`. So every parallel agent,
   whatever provider, finds the harn MCP server and writes skills/decisions/
   scratchpad into the ONE shared task context — not an empty per-worktree one.
4. **Run all N steps concurrently** (a `concurrent.futures.ThreadPoolExecutor`;
   each thread blocks on its own `adapter.run_turn(prompt, cwd=worktree)` /
   `run_feedback(command, cwd=worktree)` subprocess — real OS-level
   parallelism, the GIL is released during subprocess waits). Each step's
   prompt is built exactly as today (`_build_step_prompt`), with its own
   `Agent:`/`Model:` — a wave may mix providers.
5. **Capture each step's diff as a patch** once it finishes: the diff of its
   worktree against `base_ref`, saved as a hidden ref
   (`refs/harn/patches/<task_id>/<step_id>`, same pinning trick as checkpoints)
   AND recorded on the task ledger (`step_results[step_id]["patch_ref"]`).

### Merge into the main tree (no commits)

After all steps in the wave finish, apply their patches to the main working
tree ONE AT A TIME, in step order, via `git apply --3way`:
- Clean apply → next patch.
- Conflict → dispatch ONE **agent-merge turn**: a normal agent turn whose
  prompt carries (a) what each conflicting step was asked to do, (b) the
  conflicting hunks / `<<<<<<<` markers, and instructs it to resolve them
  coherently. It resolves in the main tree. If the merge agent is unsure it
  calls `ask_user` (standard HIL) — the run blocks exactly like any other
  blocking turn.

harn still never creates git commits — the merged result lives in the working
tree as an uncommitted diff, preserving the existing "your git history is
yours" invariant. Worktrees are removed (`git worktree remove --force`) after
their patch is captured.

### Independent rollback

Each parallel step's patch ref is its independent undo unit:
- **Roll back one step** = reverse-apply its patch (`git apply -R --3way
  <patch>`) to the main tree. Its siblings' edits stay untouched.
- **If the reverse-apply fails** (someone edited the same lines AFTER the
  merge, so the patch no longer cleanly reverses): fall back to rolling the
  WHOLE wave back to `base_ref` (`gitutil.rollback_to(base_ref, ...)`), which
  ALWAYS works — and report clearly that the single-step rollback wasn't
  possible so the whole wave was reverted, rather than silently corrupting the
  tree. (User-approved behavior.)

Studio Rerun of one parallel step: reverse-apply that step's patch (or
whole-wave fallback), re-run just that step in a fresh worktree off the current
base, re-capture its patch, re-merge.

### Blocking (`ask_user`) inside a wave

A human cannot sanely field N simultaneous mid-wave questions, so parallel-wave
agents are told, via a prompt addition, that they run CONCURRENTLY and should
avoid `ask_user` — decide autonomously with current best practices and record
the assumption via `record_decision` (the same guidance `--auto` mode already
injects, applied per-wave regardless of the run's overall auto setting). If a
parallel step nonetheless writes `BLOCKED.md`:
- that step's patch is NOT applied,
- its siblings' patches merge normally,
- the run goes BLOCKED on that one step after the wave,
- on answer, the resume re-runs JUST that step SEQUENTIALLY (off the merged
  base), where it can block/ask normally.

This keeps `_handle_block`'s single-`BLOCKED.md` model intact — at most one
step's block is ever "live" at the harness level, because unresolved parallel
blocks are deferred to a sequential re-run rather than handled concurrently.
This subsection is the part of the design most likely to want refinement once
it meets real workflows; it is deliberately conservative for Phase 3.

### Single shared context without write conflicts

All parallel agents point at the ONE main `harn_env` (via the absolute
`HARN_ENV_DIR` from step 3), so `save_to_skill` / `save_service` /
`record_decision` / `set_scratchpad` / task-ledger writes all land in the same
place — that IS the "единый контекст" requirement. Because N separate agent
processes (each with its own harn MCP server subprocess) may write
concurrently, every harn_env write path those tools hit must be guarded by the
existing cross-process file lock (`tasks._claim_lock`) — the plan must audit
`save_to_skill`/`save_service`/`record_decision`/`set_scratchpad`/`_save` and
extend the lock to any that isn't already covered. `events.jsonl` is
append-only (atomic small appends) and needs no lock. Scratchpad is
last-writer-wins by design (it's working memory, not a merge target) — the lock
just prevents a torn write, not a semantic merge.

### Agent-agnostic guarantee (explicit)

The feature is provider-neutral by construction:
- Execution is `subprocess` of whatever CLI the step's `Agent:` selects, with
  `cwd` = its worktree. The CLI is oblivious to the concurrency.
- Every connector variant is replicated into every worktree, and the harn MCP
  server's `HARN_ENV_DIR` is made absolute there — so Claude Code (`.mcp.json`),
  Cursor (`.cursor/mcp.json`), and home-dir agents (Codex/Qwen/Antigravity,
  which read `~/.codex/config.toml` etc. — already absolute/global, unaffected
  by cwd) all reach the same harn brain.
- A wave may mix providers per step; the merge/rollback layer is pure git and
  knows nothing about which agent produced a patch.

## Testing

- `workflow.py`: parse/compose round-trip for `Parallel:`; field defaults `""`.
- `loop.py` wave detection: consecutive same-group steps form a wave; a lone
  `parallel` step runs as a normal single step; non-consecutive same-group ids
  do NOT merge across an intervening different step (they're separate waves).
- Worktree lifecycle (real git tmp repo): N worktrees created off the dirty-tree
  checkpoint; each carries the base state; removed after patch capture; no
  leftover worktrees or refs on success OR on mid-wave failure.
- Connector replication: a worktree contains copied `.mcp.json`/`AGENTS.md`/
  `CLAUDE.md`, and the copied `.mcp.json`'s `HARN_ENV_DIR` is the absolute main
  harn_env path (not `"harn_env"`).
- Concurrency-safety: two threads both calling a lock-guarded harn_env write
  (e.g. `save_to_skill`) never lose an entry / corrupt the file.
- Merge: disjoint-file patches apply clean; same-file-conflicting patches
  trigger exactly one agent-merge turn; an unsure merge agent's `ask_user`
  blocks the run.
- Independent rollback: reverse-apply removes only that step's edits; a
  no-longer-reversible patch falls back to whole-wave revert and SAYS so.
- Config-error: a parallel step with `On fail:` set logs a config_error and is
  treated as if `On fail` were unset.
- Agent-agnostic: a wave with two steps whose `Agent:` differ (e.g. `claude`
  and `cursor`, both faked in tests) each run in their own worktree and both
  patches merge — no provider-specific branch in the wave/merge code.
