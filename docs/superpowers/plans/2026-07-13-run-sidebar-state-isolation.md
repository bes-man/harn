# Run Sidebar State Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve execution-sidebar interaction state while ensuring clean restarts cannot display prior-run context.

**Architecture:** Centralize client runtime reset and gate every polling render through a stable data key. Preserve keyed nested scroll positions only when changed data requires a real render.

**Tech Stack:** Python embedded HTML, vanilla JavaScript, pytest, Playwright.

## Global Constraints

- Resume keeps current context.
- Clean restart and replay clear all prior runtime context.
- Polling remains live at 1.5 seconds.
- Bump harn patch version.

---

### Task 1: Runtime state reset

**Files:** `harn/studio.py`, `tests/test_studio.py`

- [ ] Add failing tests for one reset function used by all clean-restart buttons and not by Resume.
- [ ] Implement reset of transcript, cursor, progress, disclosure sets, render key, and error state.
- [ ] Run focused tests.

### Task 2: Stable execution rendering

**Files:** `harn/studio.py`, `tests/test_studio.py`

- [ ] Add failing tests for keyed rendering, interaction deferral, and nested scroll restoration.
- [ ] Route both Board and Flow polling paths through the keyed renderer.
- [ ] Preserve keyed transcript scroll positions across required renders.
- [ ] Run Studio and full regressions, bump version, and verify through Playwright.
