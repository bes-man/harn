# Command steps + On-fail transitions (Phase 2)

## Problem

Phase 1 (shipped, 0.14.0) made every workflow step an agent turn — arbitrary
step count, per-step agent/model. Two things were deliberately deferred as
non-goals:

- A step that just runs a shell command (tests, lint, a deploy check) — no
  LLM, no tokens, no agent/model to configure — currently has to be an agent
  turn that calls a tool, which is slower and costs tokens for a
  deterministic, no-judgment action.
- Automatic "on failure, do something" — today a command's result just lands
  in context and whatever agent step runs next has to notice and react on its
  own initiative; there's no structured failure-handling path.

## Non-goals

- `On fail` only applies to command steps in this phase. Agent steps already
  have full judgment inside their own prompt (they can call `ask_user`, write
  `BLOCKED.md`, or note a problem in context for the next step) — adding a
  parallel "agent step failed" detection would require inventing a verdict
  format with no clear existing convention, for uncertain benefit. Deferred
  indefinitely, not just to a "Phase 3" — revisit only if a real workflow
  needs it.
- No new UI concept beyond a Type toggle + two fields. No parallel step
  execution (still a separate, later idea).

## Design

### Data model — two new optional per-step fields (WORKFLOW.md)

```
## 4. Tests
Type: command
Command: npm test
On fail: 3. Fix tests
```

- `Type:` — `agent` (default, omit the line) or `command`.
- `Command:` — a shell command string, meaningful only for `Type: command`.
- `On fail:` — written in WORKFLOW.md as the target step's TITLE (human-
  readable, matches how `Skills (required: …)` etc. read), but the in-memory
  node dict stores `on_fail: <target step's Id>` — resolved title→id on
  `parse()` (look up the id of the step whose title matches; no match, e.g. a
  stale reference to a renamed/deleted step → `on_fail: ""`, degrade to
  no-on_fail) and back id→title on `compose()` (look up the CURRENT title of
  the step with that id; id no longer exists → omit the `On fail:` line
  entirely rather than writing a dangling reference). This mirrors Phase 1's
  `Id:`-is-the-durable-key precedent: storage is by id, display is by title,
  survives renames the same way `stage_checkpoints`/`step_results` do.
  Meaningful only for `Type: command`. Optional even on a command step: a
  command step with no `On fail:` that fails just records the failure in
  context, same as today's Phase-1 behavior (no special handling).

### Command execution

Reuse `harn/feedback.py`'s existing `run_feedback(command, cwd, timeout=600)`
— it already does exactly what's needed: `shlex.split` + `subprocess.run`,
`ok = returncode == 0`, `FileNotFoundError`/`TimeoutExpired` both caught and
folded into `ok=False`. **Failure = exit code ≠ 0 OR timeout** (both already
produce `ok=False` from `run_feedback` — no new logic needed there).

The engine step-loop, for a `Type: command` step:
1. Skip agent/model resolution entirely — no `_adapter_for_step`/`_run_turn`.
2. `_checkpoint_stage` still runs first (consistent Run/Rerun semantics for
   ANY step type, agent or command).
3. Call `run_feedback(step["command"], project_root)`.
4. Write `task.step_results[step_id] = {"status": "ok"|"failed", "output":
   result.tail(40), "started": ..., "ended": ...}` — the `output` tail is
   exactly what an agent step's prompt-building code surfaces to the NEXT
   step via the existing "prior step results" context block (Phase 1's
   `_build_step_prompt` already threads task state through; this just adds
   command output as one more thing a later step can see, no new plumbing).
5. If `ok` → advance to the next step in plan order (unchanged Phase-1 walk).
6. If not `ok` and `on_fail` resolves to a live step id → run the **on_fail
   target step** (must itself be an agent step; checked at RUN TIME since the
   plan is unvalidated data — if the target is a command step or the id no
   longer resolves to any step, treat as a config error and fall through to
   "no on_fail" behavior, logging a warning event, never crashing the run)
   with
   an extra context block: "## Triggered by a failed step\n**{failing step
   title}** failed ({exit_code_or_timeout}): \n```\n{output tail}\n```". This
   runs through the SAME block/ask_user/HIL machinery as any agent step —
   the handler can call `ask_user` and the loop suspends exactly like today.
7. After the handler step completes (and isn't itself blocked), the engine
   jumps BACK to the original failing command step and retries it — not to
   the step after the handler. This repeats (handler → retry command → maybe
   fail again → handler → ...) until the command succeeds, the handler
   itself gets blocked waiting on a human, or...
8. ...the run's overall `max_iterations` budget (already the loop's existing
   iteration cap, unchanged from Phase 1/pre-Phase-1) is exhausted — no new
   counter. Every step run, retry, or handler dispatch already consumes one
   iteration of the existing `for _ in range(limit)` loop in `run()`, so a
   pathological fail-loop hits the SAME wall a runaway agent retry would hit
   today. This is deliberately not a new mechanism.

### UI (studio.py)

- Each step's inspector gets a **Type: Agent / Command** toggle at the top of
  the per-step config area (above where Agent/Model currently render).
- `Type: Agent` (default): unchanged from Phase 1 — Agent/Model/Effort/Temp
  controls.
- `Type: Command`: replaces that whole block with a **Command** textarea and
  an **On fail →** dropdown listing every OTHER enabled step in the current
  plan by title (value stored as that step's id, resolved to its live title
  for display — same title-tracks-rename problem every other by-id reference
  in this codebase already solves via id-not-title storage).
- Run/Rerun buttons: unchanged, work identically for command steps (checkpoint
  before running the command, restore-then-rerun exactly like an agent step).

### Migration

A plan with no `Type:` line on any step behaves byte-for-byte like Phase 1
(every step defaults to `agent`) — fully backward compatible, no migration
needed for existing task snapshots or presets.

## Testing

- `workflow.py`: parse/compose round-trip for `Type:`/`Command:`/`On fail:`;
  `On fail:` title resolves to the target's `Id:` at parse/save time; missing
  target (renamed/deleted step) degrades to no-on_fail rather than crashing.
- `loop.py`: a command step runs via `run_feedback` (mock subprocess), writes
  the right `step_results` shape; success advances normally; failure with no
  `on_fail` just records and advances (Phase-1-equivalent); failure WITH
  `on_fail` dispatches the handler step with the extra context block, then
  retries the original command step; a fail-loop that never succeeds
  terminates at `max_iterations` (no new counter, reuses the existing one —
  assert the SAME iteration-limit message/behavior Phase 1 already has).
  Config-error case: `on_fail` pointing at another command step logs a
  warning event and behaves as if `on_fail` were unset.
- `studio.py`: Type toggle switches the rendered block; On-fail dropdown only
  lists OTHER steps (not itself); Command-type Run/Rerun round-trips through
  the same checkpoint mechanism as agent steps (one shared test parameterized
  over both types, not two copies, where the plan's existing checkpoint tests
  allow it).
