---
topic: services
summary: Service registry — per-service responsibility/standards/constraints; read for the AS-IS step.
---

# Service registry (harn_env/services/)

One file per service/module describing its **responsibility, standards, and
constraints** — duties and rules, NOT a code walkthrough. The token-cheap index
(name + one-line responsibility) tells you instantly whether a service matters
for the current task at all.

- `list_services` → scan the index; `read_service(name)` → ONLY for services
  the task touches.
- A service you touch is missing or stale? → `save_service(name, responsibility,
  content)` with `## Responsibility` / `## Standards` / `## Constraints` /
  `## Gotchas`. (Part of the post-task reconcile step.)
- Onboarding seeds the registry; every task keeps it current. This replaces
  re-indexing the repo each session.
