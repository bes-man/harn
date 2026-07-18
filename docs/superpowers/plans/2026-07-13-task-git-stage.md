# Task Git Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Roll back only changes owned by one task.

**Architecture:** Capture exact per-turn patches using a temporary Git index that never touches the repository index. Persist their ordered hidden refs on the task and reverse them transactionally without baseline fallback.

**Tech Stack:** Python, Git plumbing, pytest.

## Global Constraints

- Preserve unrelated staged, unstaged, and untracked user changes.
- Clear execution history only after successful patch rollback.
- Block safely on patch conflicts.
- Bump harn patch version.

---

### Task 1: Isolated patch capture

- [ ] Add failing tests for patch capture without global index mutation.
- [ ] Implement temporary-index checkpoints and `patch_since`.
- [ ] Persist ordered task patch refs after turns and parallel merges.

### Task 2: Task-only rollback

- [ ] Add failing tests preserving unrelated WIP and retaining refs on conflict.
- [ ] Reverse task patches in order and clear refs only on success.
- [ ] Run full regression tests and bump version.
