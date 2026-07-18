# Run Sidebar State Isolation Design

## Problem

The execution sidebar is rebuilt from several polling paths, so outer and nested scroll state is lost. Clean restart clears persistent backend data but some restart buttons keep the old client transcript, cursor, progress, and disclosure state.

## Design

- One `resetRunClientState(taskId)` clears all runtime-only browser state for a clean restart.
- Every clean-restart entry point calls it; Resume does not.
- One render key covers the selected task, frozen plan, ledger, progress, transcript, mode, and error.
- Polling calls `renderRunHistoryIfChanged()`, which skips unchanged renders and defers while the sidebar is being edited, selected, or pointer-dragged.
- Required renders preserve the outer sidebar scroll and keyed nested transcript scroll positions.

## Verification

Source tests pin every restart entry point and renderer guard. Playwright verifies nested scrolling remains stable for longer than a polling interval and that restart state contains no prior transcript entries.

