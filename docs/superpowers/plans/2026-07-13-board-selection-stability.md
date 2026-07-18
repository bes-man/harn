# Board Selection Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Board text selections and Review log scroll stable while live polling continues.

**Architecture:** Separate data polling from DOM rendering using stable render keys. Treat form editing, pointer selection, and non-collapsed text selection as panel interactions that defer changed renders until interaction ends.

**Tech Stack:** Python embedded HTML, vanilla JavaScript, pytest, Playwright.

## Global Constraints

- Polling remains live at the existing interval.
- No new frontend dependency.
- Bump harn version for `harn update`.

---

### Task 1: Board render stability

**Files:**
- Modify: `tests/test_studio.py`
- Modify: `harn/studio.py`

- [ ] Add failing assertions for selection interaction detection, data-change render gates, and Review log scroll preservation.
- [ ] Run the focused tests and confirm they fail because the guards are absent.
- [ ] Add `panelHasTextSelection`, pointer-drag tracking, `panelIsInteracting`, Board render keys, and same-task scroll restoration.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Release and browser verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `harn/__init__.py`

- [ ] Bump the patch version in both version files.
- [ ] Run the Studio regression tests and the broader test suite.
- [ ] Start Studio, select Review log text with Playwright, wait more than 1.5 seconds, verify the selection remains, and save a screenshot.
