# Board Selection Stability Design

## Problem

The Board poll replaces the task list and inspector DOM every 1.5 seconds even when the returned data is unchanged. Replacing `innerHTML` destroys browser text selections and resets nested Review log scrolling.

## Design

- Polling continues at the current interval so run progress remains live.
- The Board list and task inspector receive stable render keys derived from their data. A panel is rendered only when its key changed.
- A panel is not rendered while it contains a focused form control, an active pointer text-selection drag, or a non-collapsed browser text selection. A deferred change is rendered by the next poll after the interaction ends.
- When a changed task inspector must render, its main scroll position and Review log scroll position are restored for the same selected task.
- Review log receives a stable DOM id and remains selectable.

## Verification

- Source-level regression tests cover interaction detection, render-key gating, and scroll restoration.
- Browser automation selects Review log text, waits longer than one polling interval, and verifies that the same non-empty selection remains.

