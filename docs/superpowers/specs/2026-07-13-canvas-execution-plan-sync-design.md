# Canvas and execution-plan synchronization

## Goal

The Flow canvas and Run progress sidebar must describe the same workflow.
Editing the canvas updates the sidebar execution preview immediately. Starting
the flow from either surface freezes that exact preview as the task's execution
plan. Runtime status then updates both surfaces together, including every
member of a parallel wave.

## Plan lifecycle

The sidebar has two explicit modes:

- **Preview:** no run is active for the selected task. Nodes come directly from
  the current in-browser `S.workflow`, including unsaved edits, stable step ids,
  order, enabled state, tools/skills, and `parallel` group ids.
- **Execution:** a run has been launched. Nodes come from the task snapshot
  created from the current canvas immediately before launch. Canvas and
  sidebar use the same step ids, so progress events address both consistently.

Past task snapshots never replace an idle canvas preview. Past results remain
available as execution history but are not mixed into the next preview.

## Launch contract

`Run flow` is available in both the terminal canvas node and the sidebar. Both
buttons call the same client function and backend launch sequence:

1. Validate the selected task and ensure no other run is active.
2. Save the current canvas preset if it has unsaved edits.
3. Assign the active workflow to the task.
4. Replace the task plan snapshot with a copy of the current canvas plan.
5. Start a new execution scope, preserving prior transcript/results as history
   rather than treating them as the new plan.
6. Open the sidebar in execution mode immediately.

An execution snapshot is immutable for the duration of that run. Canvas edits
made after launch affect the next preview/run, not the active execution.

The sidebar action changes by state:

- idle preview: `Run flow`;
- active run: `Stop`;
- stopped incomplete snapshot: `Resume`;
- prior execution with baseline: `Rerun from scratch` remains a separate,
  destructive action with confirmation.

## Parallel execution plan

Consecutive enabled steps sharing a non-empty `parallel` id are rendered inside
one sidebar wave container labelled `∥ parallel · <id>`. The wave is a visual
group, not a synthetic step: totals and completion still count every member.

Each member keeps its own:

- stable step id and title;
- pending/running/complete/failed/blocked status;
- tool and skill usage indicators;
- live transcript and final result;
- retry controls where applicable.

Non-parallel steps remain normal timeline cards. A one-member `parallel` id is
rendered as a normal step because the engine does not execute it as a wave.

## Shared runtime status

`events.jsonl`/`PROG.stages`, keyed by the frozen step ids, is the single live
status source. Every polling tick:

- `applyProgress()` updates canvas node colors without rebuilding controls;
- Run progress maps the same status to the matching sidebar card;
- all active members of a parallel wave are active simultaneously;
- each member transitions independently to success or failure;
- transcript polling continues per step.

The canvas keeps showing the active execution's nodes while that run is active.
This prevents editing or switching the visible preset from creating a false
color association. After the run ends, the normal editable canvas preview
resumes.

## Error handling

- Launch is rejected before snapshot replacement if another run is active.
- A save/snapshot failure prevents launch and leaves the prior execution plan
  intact.
- Missing or stale task snapshots never override idle preview.
- Unknown progress step ids are retained in trace data but cannot color an
  unrelated canvas node.
- Sidebar launch errors are shown inline and do not close the sidebar.

## Testing

- Unit tests prove snapshot replacement uses all current canvas nodes and
  preserves parallel ids.
- Studio tests prove preview reads `S.workflow`, execution reads the frozen
  task plan, and both Run buttons use the same launch function.
- Rendering tests prove two consecutive wave members appear in one group and
  both count toward totals.
- Playwright edits a two-member parallel wave, observes immediate sidebar
  preview synchronization, launches from the sidebar, and verifies both canvas
  nodes and sidebar cards become active together before independently
  completing.

## Non-goals

- Mutating an already-running execution when the canvas changes.
- Treating a parallel wave as one result or one transcript.
- Automatically rerunning a completed task without explicit confirmation.
- Removing historical execution data.
