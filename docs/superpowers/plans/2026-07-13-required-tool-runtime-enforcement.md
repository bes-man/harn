# Required Tool Runtime Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent any step with unused required skills or tools from being recorded as successful.

**Architecture:** Reuse the existing event-scoped usage audit as the source of truth and add a shared missing-required helper. Gate sequential, parallel, and manual step completion on that result with one retry before blocking.

**Tech Stack:** Python, pytest, Harn event ledger.

## Global Constraints

- Required calls may occur before, during, or after other step work.
- Recommended items never block completion.
- No prompt wording is relied upon for correctness.
- Bump the harn patch version.

---

### Task 1: Shared completion gate

**Files:** `harn/loop.py`, `tests/test_step_enforcement.py`

- [ ] Write failing tests proving missing required tools cannot produce `ok` in sequential and manual execution.
- [ ] Add a shared helper returning exact missing required usage names.
- [ ] Restore one-retry-then-block behavior for sequential execution and enforce manual execution.
- [ ] Run focused tests.

### Task 2: Parallel enforcement and release

**Files:** `harn/loop.py`, `tests/test_parallel_steps.py`, `pyproject.toml`, `harn/__init__.py`

- [ ] Write a failing parallel-wave test where an agent returns an answer without calling its required tool.
- [ ] Audit every wave member and prevent missing-required members from being ledgered as `ok`.
- [ ] Bump the patch version and run full regressions.
