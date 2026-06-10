---
name: ui
description: UI/UX conventions, design tokens, components, accessibility. Load only for user-facing/frontend work.
---

# ui

Capture design system, tokens, component patterns and accessibility requirements. Read only for frontend/user-facing tasks.

Tagging a task with this skill marks it **user-facing**: harn then expects an
approved design mockup (`harn_env/design/<task>.html`, via `save_design`) before
implementation, and runs the browser-verification phase (Playwright MCP) on it
when `[browser]` is enabled.
