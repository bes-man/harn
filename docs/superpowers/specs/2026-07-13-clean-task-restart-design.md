# Clean Task Restart Design

## Goal

Make a repeated **Run Flow** a destructive clean restart, while keeping **Resume** as the only continuation action.

## Behavior

- A task with no baseline starts normally and does not show a warning.
- A task with a baseline shows a confirmation before Run Flow: project changes will be restored with git and saved execution context will be lost.
- Confirmed restart restores the project to the task baseline before launching.
- Restart clears scratchpad, decisions, step results and attempts, stage checkpoints, pending blocked state, and saved transcript entries for the task.
- The current canvas remains authoritative: after cleanup, Run Flow freezes and launches the posted canvas plan.
- Resume keeps the frozen plan and all existing task context.
- Rerun from scratch uses the same cleanup primitive before relaunching its frozen plan.
- If rollback fails, launch is rejected and the saved execution context is retained.

## Implementation

Add one backend cleanup primitive used by both `launch_workflow` restart and `rerun_workflow`. Extend transcript storage with task-scoped deletion. The frontend determines destructive restart from the selected task's `baseline_ref` and shows a warning only in that case.

## Verification

Regression tests must prove git restoration, transcript deletion, cleared task state, no retained attempts, rollback-failure safety, conditional warning copy, and unchanged Resume behavior.
