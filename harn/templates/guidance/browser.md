---
topic: browser
summary: Verify UI work in a real browser via Playwright MCP before submitting.
---

# Verify UI work in a real browser

When the Playwright MCP tools (`browser_navigate`, `browser_snapshot`,
`browser_click`, `browser_take_screenshot`, …) are available and the task is
user-facing, drive the running app like a user:

- Walk each acceptance criterion against the running app.
- Compare against the approved design (`read_design`).
- Save screenshots to `harn_env/state/screenshots/<task_id>/`.

In headless runs harn starts/stops the app itself (`[browser]` in
`harn_env/harn.toml`) and runs this as its own phase. In chat mode, do it
yourself before `submit_for_review`. `UI: FAIL` (headless) loops the task back.
