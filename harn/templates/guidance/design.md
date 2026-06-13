---
topic: design
summary: Design-before-code for user-facing tasks — HTML mockup as the visual contract.
---

# Design before code (user-facing tasks)

If a task changes anything the user will SEE, generate a single-file static
HTML mockup of the final interface FIRST and save it with
`save_design(task_id, html)` — it lands in `harn_env/design/<task_id>.html`.

- The mockup must be self-contained (inline CSS, realistic sample data) and
  show every state the acceptance criteria mention (empty, error, success…).
- Ask the human to open it and confirm via `ask_user`, iterating until approved.
- The approved mockup is the visual contract: build to it, and tag the task
  with the `ui` skill (`update_task`) so the browser-verification phase runs.
- `read_design(task_id)` returns it later (executor + oracle build/verify
  against it).
