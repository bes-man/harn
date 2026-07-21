# Studio answer auto-resume design

## Goal

When a user answers an agent's blocked question in the Studio task comments,
the task resumes automatically instead of requiring a separate launch.

## Scope

`studio.answer_payload()` will:

1. Read the currently blocked task id from persisted loop state.
2. Record the answer through `loop.answer()` as it does today.
3. Start that task through the existing `runner.launch()` path.

The persisted blocked task id is authoritative; the task selected in the UI is
not used to decide which task resumes.

## Failure handling

If another Studio run is active, the answer remains saved and the API returns a
warning that the task was not restarted. This avoids losing the human response
or launching two loops concurrently.

## Out of scope

This does not restart an agent operating in an external interactive chat. It
only resumes headless task runs owned by Studio.

## Verification

Add a regression test that creates a blocked task, submits an answer, and
asserts that `runner.launch()` is called with that task. Keep the existing test
that verifies the answer clears `BLOCKED`. Verify the Studio UI still submits
comments through the same endpoint in a browser.
