# Single Import button for tools + skill view/export (Phase 7)

## Problem

Studio's Tools tab currently has two separate buttons for adding a custom
tool: "＋ Upload tool" (pick a script, then manually type name/description/
params) and "＋ Import tool" (load a bundle another harn user exported).
The user wants ONE button, "＋ Import", that figures out which case applies
from the file itself.

Separately, clicking a skill in the Skills tab opens an editor (name/
description/body, Save/Delete) but offers no way to view it read-only or
export it to share with a colleague — unlike custom tools, which already
have Export.

## Design

### Tools: one "＋ Import" button

- Remove the two separate buttons/inputs; add one "＋ Import" button +
  hidden file input.
- On file pick, the client peeks at the file to decide which existing
  backend flow to use — the SAME two flows Phase 5 already built, just
  chosen automatically instead of by which button was clicked:
  - Filename ends in `.zip`, OR the file's content parses as JSON with
    `name`+`command` keys present → treat as a tool BUNDLE: POST to
    `/api/tools/import` exactly as today's "Import tool" button already
    does.
  - Anything else (a `.sh`/`.py`/any other script) → treat as a raw
    SCRIPT: run the existing "Upload tool" flow unchanged (prompt for
    name/description/params, POST to `/api/tools/save` with
    `script_name`/`content_b64`, command auto-built as
    `bash <script> {params}`).
- The backend is unchanged and remains authoritative — if the client's
  guess is wrong (e.g. a `.json` file that isn't actually a valid tool
  bundle), `/api/tools/import` already returns a clear `{"ok": false,
  "error": ...}` today; no new backend risk.
- Each tool row's existing "Export" button is unchanged.

### Skills: view + export

- Clicking a skill still opens the existing editor. Add two things to it:
  - A read-only "View" mode is already effectively what the editor shows
    before any edits — no new mode needed; the ask is really "export,"
    since viewing already works via the existing editor.
  - An "Export" button next to the existing Save/Delete controls:
    downloads that skill's `SKILL.md` file verbatim (frontmatter +
    body — the exact file already on disk, unmodified) as
    `<name>.md`. New backend route `GET /api/skill/export?name=...`
    streams the file's raw bytes, mirroring the tools Export route's
    `_send(..., extra_headers=...)` pattern from Phase 5.
- No skill Import in this phase — the user only asked for view+export
  (skills already have no name-uniqueness gate the way tools do; adding
  import would need its own collision-handling design, deferred).

## Non-goals

- Skill import (deferred — not requested).
- Any change to custom tools' underlying `save`/`import_bundle`/execution
  logic (Phase 5's security-reviewed code) — this phase only changes which
  UI button triggers which already-existing backend call.

## Testing

- Tools: a client-side `looksLikeBundle(filename, text)`-style helper (or
  equivalent) correctly classifies a `.zip` filename, a valid tool-bundle
  `.json` file, and a `.sh` script — covered by inspecting the JS logic
  (this repo's studio.py has no JS unit-test harness; verified via
  `node --check` + live/manual click-through, matching every prior UI
  task's verification method in this project).
- Skills: `GET /api/skill/export?name=<name>` returns the exact bytes of
  that skill's `SKILL.md`; a nonexistent skill name returns 404, not a
  crash.
