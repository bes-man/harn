---
topic: parallel
summary: Run independent tasks across multiple agents; depends_on, atomic claims, worktrees.
---

# Parallel work (multiple agents)

**Proactively offer parallelism.** After creating tasks (or when picking one
up), check `runnable_tasks` — `get_next_task` also tells you when others are
runnable. If **2+ independent tasks** are runnable, TELL the user: "PRJ-002 and
PRJ-003 don't depend on each other — we can run them in parallel. Want to?" Then:
- If your runtime can spawn subagents (e.g. Claude Code's Task/Agent tool),
  offer to fan them out — one subagent per task, each calling
  `get_next_task(worker="w1"|"w2"|…)` so claims don't collide.
- Otherwise tell the user to open extra agent windows / `harn run` processes.
Don't silently work tasks one-by-one when the board is parallelizable.

Tasks run in parallel when they don't depend on each other:
- **Declare real ordering** with `create_task(..., depends_on=["PRJ-001"])` —
  a task becomes runnable only once ALL its `depends_on` ids are `done`. Use
  this ONLY for genuine constraints (build the API before wiring the UI to it);
  for soft "do this sooner" preference use `priority`, not a dependency.
- **Leave independent tasks with no `depends_on`** so several agents can take
  them at once.
- **`get_next_task` claims atomically** per `worker` id — two agents never get
  the same task; you always get your own in-progress task back.

Give parallel agents separate git worktrees if their tasks touch overlapping
files, so their edits don't collide.
