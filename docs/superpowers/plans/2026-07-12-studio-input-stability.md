# Studio Input Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep Studio form controls focused and intact while the 1.5-second live-status polling continues.

**Architecture:** The browser client will treat each rendered panel as immutable while it contains an active form control. Poll responses will be sequenced so an older response cannot overwrite a newer UI state, and periodic updates will modify only live-status DOM elements rather than reconstructing form panels.

**Tech Stack:** Python 3.9, embedded vanilla JavaScript in `harn/studio.py`, pytest, Playwright MCP.

## Global Constraints

- Preserve existing uncommitted user changes in `harn/studio.py` and `tests/test_settings_save.py`.
- Keep the 1.5-second data-refresh cadence.
- Do not add a frontend dependency or a build step.
- Verify the rendered Studio UI using Playwright, including focus and typed value after multiple polling ticks.

---

### Task 1: Protect panel rendering during user input

**Files:**
- Modify: `harn/studio.py` (embedded Studio client polling and panel render guards)
- Test: `tests/test_studio.py`

**Interfaces:**
- Consumes: `document.activeElement`, `pollBoard()`, `pollProgress()`, `renderBoard()`, `renderTaskDetail()`.
- Produces: a single `panelIsEditing(element)` predicate and deferred rendering behavior for live polled panels.

- [x] **Step 1: Write the failing regression test**

Add a Studio HTML contract test that asserts the client contains a shared panel-edit predicate and that both polling paths call it before replacing a panel with `innerHTML`.

```python
def test_studio_polling_defers_panel_renders_while_editing(tmp_path):
    html = studio._HTML
    assert "function panelIsEditing(el)" in html
    assert "if(!NEW_TASK_OPEN && !panelIsEditing($('#listView'))) renderBoard();" in html
    assert "if(!panelIsEditing($('#insp'))) renderTaskDetail();" in html
```

- [x] **Step 2: Run the regression test to verify it fails**

Run: `pytest tests/test_studio.py::test_studio_polling_defers_panel_renders_while_editing -q`

Expected: FAIL because the shared predicate and/or the guarded render calls do not yet exist in the expected form.

- [x] **Step 3: Implement the smallest shared guard**

Replace the narrow focus check with `panelIsEditing(el)`, which recognizes `INPUT`, `TEXTAREA`, `SELECT`, and content-editable descendants. Make every polling-triggered `innerHTML` render of `#listView`, `#insp`, and the blocked-answer panel defer when that panel is being edited. Keep live indicators updated in place.

```javascript
function panelIsEditing(el){
  const active=document.activeElement;
  return !!(active && el && el.contains(active) &&
    (/^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) || active.isContentEditable));
}
```

- [x] **Step 4: Run the regression test to verify it passes**

Run: `pytest tests/test_studio.py::test_studio_polling_defers_panel_renders_while_editing -q`

Expected: PASS.

### Task 2: Make polling monotonic and verify the UI behavior

**Files:**
- Modify: `harn/studio.py` (embedded Studio client polling state)
- Test: `tests/test_studio.py`

**Interfaces:**
- Consumes: `fetch()`, `BOARD`, `PROG`, periodic timers.
- Produces: monotonically applied board/progress responses and a Playwright regression check.

- [x] **Step 1: Write the failing regression test**

Extend the HTML contract test to require request generation counters for `pollBoard()` and `pollProgress()`, with an early return when a response is no longer current.

```python
def test_studio_polling_discards_stale_responses(tmp_path):
    html = studio._HTML
    assert "let boardPollGeneration=0" in html
    assert "if(generation!==boardPollGeneration) return;" in html
    assert "let progressPollGeneration=0" in html
    assert "if(generation!==progressPollGeneration) return;" in html
```

- [x] **Step 2: Run the regression test to verify it fails**

Run: `pytest tests/test_studio.py::test_studio_polling_discards_stale_responses -q`

Expected: FAIL because polling responses currently have no generation guard.

- [x] **Step 3: Implement response sequencing**

Increment a generation counter before each fetch and apply the response only when its counter is still current. This prevents a slow prior poll from replacing the UI after a later poll has completed.

```javascript
let boardPollGeneration=0;
async function pollBoard(){
  const generation=++boardPollGeneration;
  const next=await (await fetch(api('/api/board'))).json();
  if(generation!==boardPollGeneration) return;
  BOARD=next;
  // existing guarded live updates
}
```

- [x] **Step 4: Run the focused Python tests**

Run: `pytest tests/test_studio.py -q`

Expected: PASS.

- [x] **Step 5: Run Playwright UI regression**

Start a temporary `harn ui` server, focus a Studio input, type a distinctive value, wait through at least three 1.5-second cycles, then assert the same input is focused and retains the value. Capture an accessibility snapshot and screenshot as evidence.

```javascript
const input = page.locator('#bodyTA');
await input.fill('focus-regression-value');
await page.waitForTimeout(5000);
await expect(input).toBeFocused();
await expect(input).toHaveValue('focus-regression-value');
```

- [x] **Step 6: Run the complete test suite**

Run: `pytest -q`

Expected: PASS with zero failures.
