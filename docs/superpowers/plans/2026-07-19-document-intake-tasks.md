# Document Intake → Tasks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A document sent to the Telegram bot or POSTed to `/api/tasks/intake` becomes a task with the file attached, optionally routed to a role, behind an explicit confirm-before-run gate.

**Architecture:** Config first (`[intake] confirm_before_run`), then the Telegram file-receive primitives (`download_file` + `poll_documents`), then the shared `intake.py` module (create-task-from-document + optional dispatch + confirm gate) that both channels call, then wire the Telegram channel into `watch`, then the HTTP multipart route (behind the auth gate feature B added), then verification.

**Tech Stack:** Python stdlib. **Multipart parsing must NOT use `cgi`** (removed in Python 3.13+; harn runs on 3.14) — use `email`/a minimal manual boundary parser. Existing harn conventions (attachments.save, TelegramHIL, triggers.dispatch_command). No new pip dependency.

## Global Constraints

- No new third-party pip dependency; no `cgi` module.
- The HTTP intake route sits in feature B's protected set (`/api/tasks/intake` is already in `_PROTECTED_ROUTES`) — the route handler just needs to exist.
- A plain intake with no agent to run never needs a confirm gate (nothing to confirm).
- `confirm_before_run` defaults `true` (safe: a document-triggered agent run asks first).
- No document-CONTENT parsing (OCR/extraction) — the file is attached; the agent reads it via `read_attachment`. Out of scope.
- Follow `docs/superpowers/specs/2026-07-19-document-intake-tasks-design.md`.

---

### Task 1: `[intake] confirm_before_run` config

**Files:** Modify `harn/config.py`; Test `tests/test_document_intake.py` (new).

**Interfaces:** Produces `Config.intake_confirm_before_run: bool` (default `True`), parsed from `[intake] confirm_before_run`.

- [ ] **Step 1: failing tests**
```python
# tests/test_document_intake.py
"""Document intake spec (docs/superpowers/specs/2026-07-19-document-intake-tasks-design.md)."""
from __future__ import annotations
from harn.config import Config
from harn import ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME; env.mkdir(); return env


def test_intake_confirm_defaults_true(tmp_path):
    assert Config.load(_env(tmp_path)).intake_confirm_before_run is True


def test_intake_confirm_from_toml(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text("[intake]\nconfirm_before_run = false\n", encoding="utf-8")
    assert Config.load(env).intake_confirm_before_run is False
```
- [ ] **Step 2:** `python3 -m pytest tests/test_document_intake.py -v` → FAIL (no attribute).
- [ ] **Step 3:** In `harn/config.py`: `DEFAULTS["intake"] = {"confirm_before_run": True}`; `Config` field `intake_confirm_before_run: bool = True`; load: `intake_confirm_before_run=bool((data.get("intake", {}) or {}).get("confirm_before_run", True))`.
- [ ] **Step 4:** tests pass (2).
- [ ] **Step 5:** full suite (`python3 -m pytest -q`).
- [ ] **Step 6:** `git add harn/config.py tests/test_document_intake.py` → commit `feat(config): [intake] confirm_before_run setting`.

---

### Task 2: `telegram.download_file` + `poll_documents`

**Files:** Modify `harn/telegram.py`; Test `tests/test_document_intake.py`.

**Interfaces:**
- Consumes: existing `_api` (Telegram API call), `_get_updates`, `_load_offset`/`_save_offset`, the trusted `chat_id` check pattern from `poll_commands`.
- Produces: `TelegramHIL.download_file(file_id) -> bytes | None` (`getFile` → file_path → download via `https://api.telegram.org/file/bot<token>/<path>`; best-effort, None on failure). `TelegramHIL.poll_documents(state_dir) -> list[dict]` — non-blocking drain returning, for each `message.document`/`message.photo` from the TRUSTED chat, `{"file_id","filename","caption","message_id"}`. Shares the offset file with `poll_commands` (so the two never reprocess each other's updates — CAUTION: draining updates in poll_commands vs poll_documents must be coordinated; see Step 3).

- [ ] **Step 1: failing tests** (stub `_api`/`_http` and `_get_updates` like existing telegram tests)
```python
import harn.telegram as tg
from harn.telegram import TelegramHIL


def _doc_update(update_id, chat_id, file_id, caption="", is_bot=False):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id},
            "from": {"is_bot": is_bot},
            "document": {"file_id": file_id, "file_name": "report.pdf"},
            "caption": caption}}


def test_poll_documents_only_trusted_chat(monkeypatch, tmp_path):
    batch = {"ok": True, "result": [
        _doc_update(1, 999, "F-foreign"),          # foreign chat → ignored
        _doc_update(2, 42, "F-good", "analyze this"),  # trusted → kept
        _doc_update(3, 42, "F-bot", is_bot=True),  # bot → ignored
    ]}
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: batch)
    hil = TelegramHIL(token="t", chat_id="42")
    docs = hil.poll_documents(tmp_path)
    assert [d["file_id"] for d in docs] == ["F-good"]
    assert docs[0]["caption"] == "analyze this"
    assert docs[0]["filename"] == "report.pdf"


def test_download_file_returns_bytes(monkeypatch, tmp_path):
    # getFile returns a file_path; the file download returns bytes.
    monkeypatch.setattr(TelegramHIL, "_api",
        lambda self, method, params, timeout=15: {"ok": True, "result": {"file_path": "docs/x.pdf"}})
    monkeypatch.setattr(tg, "_http_get_bytes", lambda url, timeout: b"PDFDATA", raising=False)
    hil = TelegramHIL(token="t", chat_id="42")
    data = hil.download_file("F-good")
    assert data == b"PDFDATA"
```
- [ ] **Step 2:** run → FAIL (no `poll_documents`/`download_file`/`_http_get_bytes`).
- [ ] **Step 3:** Implement in `harn/telegram.py`:
  - `_http_get_bytes(url, timeout) -> bytes | None` (module-level, mirrors `_http_post_json`'s error handling: urllib GET, returns raw bytes or None on any failure).
  - `download_file(self, file_id)`: `body = self._api("getFile", {"file_id": file_id})`; `path = body["result"]["file_path"]`; `return _http_get_bytes(f"https://api.telegram.org/file/bot{self.token}/{path}", 30)`. Guard every step (None on any miss).
  - `poll_documents(self, state_dir)`: read offset, `_get_updates(offset, 0)`, save offset, filter to trusted-chat non-bot messages having a `document` or `photo`, return the shaped dicts. For a `photo`, use the largest size's `file_id` and a synthesized filename (e.g. `f"photo-{message_id}.jpg"`).
  - **Offset coordination:** `poll_commands` and `poll_documents` both advance the SAME offset file. If watch calls both each tick, whichever runs first consumes ALL updates and advances the offset, so the second sees nothing. Fix: make ONE drain that returns BOTH commands and documents — OR have watch call a single new method. Simplest robust approach: add `poll_updates(state_dir) -> dict` returning `{"commands": [...], "documents": [...]}` in one drain, and refactor `poll_commands` to delegate to it (keeping `poll_commands` for existing callers/tests). Implement `poll_updates` as the single drain; `poll_documents` can be `poll_updates(...)["documents"]` and `poll_commands` `poll_updates(...)["commands"]` — BUT that double-drains. Cleanest: `poll_updates` is the real drain; watch (Task 4) calls `poll_updates` ONCE and dispatches both lists. Keep `poll_commands`/`poll_documents` as thin wrappers ONLY for tests, each doing its own drain (fine in isolation). Document this clearly so Task 4 uses the single `poll_updates`.
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** full suite (confirm existing telegram tests + the triggers/watch tests still pass — you added methods, didn't change `poll_commands`'s behavior).
- [ ] **Step 6:** `git add harn/telegram.py tests/test_document_intake.py` → commit `feat(telegram): download_file + poll_documents/poll_updates for file intake`.

---

### Task 3: `intake.py` — create task from a document (+ optional dispatch + confirm gate)

**Files:** Create `harn/intake.py`; Test `tests/test_document_intake.py`.

**Interfaces:**
- Consumes: `attachments.save`, `tasks.create_task`, `tasks.set_status`, `triggers.dispatch_command`, `TelegramHIL.await_answer` (confirm gate), `Config.intake_confirm_before_run`.
- Produces: `intake(project_root, env_dir, *, filename, data, text="", agent=None, cfg=None) -> dict` — saves the attachment to a NEW task (title = first line of `text`, description = `text`), and if `agent` is given: run the confirm gate (unless `confirm_before_run=False`), then `triggers.dispatch_command(agent, task_id)`. Returns `{"ok":True,"task_id":...,"pr_url"?...}` or `{"ok":False,"error":...}`.

- [ ] **Step 1: failing tests** (unit — stub the dispatch + confirm)
```python
def test_intake_creates_task_and_attaches_file(tmp_path):
    from harn import intake, tasks, attachments, scaffold, ENV_DIRNAME
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    r = intake.intake(tmp_path, env, filename="report.pdf", data=b"PDF",
                      text="Investigate the outage\ndetails here")
    assert r["ok"] is True
    t = tasks.find(env, r["task_id"])
    assert t.title == "Investigate the outage"
    files = attachments.list_files(env, t.id)
    assert any(f["name"] == "report.pdf" for f in files)


def test_intake_with_agent_confirm_off_dispatches(tmp_path, monkeypatch):
    from harn import intake, scaffold, ENV_DIRNAME, triggers
    from harn.config import Config
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "harn.toml").write_text("[intake]\nconfirm_before_run = false\n", encoding="utf-8")
    called = {}
    monkeypatch.setattr(intake.triggers_mod, "dispatch_command",
        lambda pr, ed, cmd, arg, cfg=None: called.setdefault("cmd", cmd) or {"ok": True, "task_id": arg, "status": "done"})
    r = intake.intake(tmp_path, env, filename="f.txt", data=b"x", text="do it", agent="analyst")
    assert r["ok"] is True
    assert called["cmd"] == "analyst"


def test_intake_confirm_decline_does_not_dispatch(tmp_path, monkeypatch):
    from harn import intake, scaffold, ENV_DIRNAME
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME  # confirm_before_run defaults True
    # confirm gate returns "no"
    monkeypatch.setattr(intake, "_confirm", lambda env_dir, cfg, summary: False)
    dispatched = {"n": 0}
    monkeypatch.setattr(intake.triggers_mod, "dispatch_command",
        lambda *a, **k: dispatched.update(n=dispatched["n"] + 1) or {"ok": True})
    r = intake.intake(tmp_path, env, filename="f.txt", data=b"x", text="do it", agent="analyst")
    assert dispatched["n"] == 0            # declined → never dispatched
    assert r["ok"] is True and r.get("confirmed") is False
```
- [ ] **Step 2:** run → FAIL (no module).
- [ ] **Step 3:** Create `harn/intake.py`:
  - `intake(...)`: `attachments.save` first? No — create the task first (need task_id), then `attachments.save(env_dir, task.id, filename, data)`. Title = first non-empty line of `text` (fallback `filename`); description = `text`. If `agent`: resolve `cfg = cfg or Config.load(env_dir)`; if `cfg.intake_confirm_before_run` and NOT `_confirm(env_dir, cfg, summary)` → return `{"ok":True,"task_id":...,"confirmed":False}` (task+file kept, not run); else `triggers.dispatch_command(project_root, env_dir, agent, task.id, cfg=cfg)` and return its result merged with task_id.
  - `_confirm(env_dir, cfg, summary) -> bool`: post via `TelegramHIL.from_env(env_dir).await_answer(summary + " — reply 'yes' to run", ...)`; interpret a yes/да as True, anything else False; if Telegram unconfigured, default to True (a local/API caller with confirm on but no Telegram channel shouldn't hang — document this choice). Keep it small and guarded.
- [ ] **Step 4:** tests pass (3).
- [ ] **Step 5:** full suite.
- [ ] **Step 6:** `git add harn/intake.py tests/test_document_intake.py` → commit `feat(intake): create a task from a document, optional dispatch behind a confirm gate`.

---

### Task 4: Wire the Telegram document channel into `watch`

**Files:** Modify `harn/loop.py` (the watch tick's Telegram poll block); Test `tests/test_document_intake.py`.

**Interfaces:** Consumes `poll_updates` (Task 2) + `intake.intake` (Task 3). The watch tick drains updates ONCE via `poll_updates`, dispatches commands (existing behavior) AND documents (new: for each doc, `download_file` then `intake.intake` with the caption as text and, if the caption is a slash command, that role as `agent`).

- [ ] **Step 1: failing test** — a `watch(..., _once=True)` tick with a fake TelegramHIL whose `poll_updates` returns one document; assert `intake.intake` is called with the downloaded bytes and a task gets created.
```python
def test_watch_tick_routes_a_telegram_document_to_intake(tmp_path, monkeypatch):
    from harn import loop, scaffold, ENV_DIRNAME, roles
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(exist_ok=True)
    (env / "agents" / "analyst.md").write_text("---\nname: analyst\nstatus: todo\n---\n## Role\na", encoding="utf-8")
    intook = {}
    class FakeHIL:
        def poll_updates(self, sd): return {"commands": [], "documents":
            [{"file_id": "F", "filename": "r.pdf", "caption": "/analyst reproduce", "message_id": 1}]}
        def download_file(self, fid): return b"BYTES"
        def send(self, *a, **k): return 1
    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda *_: FakeHIL()))
    monkeypatch.setattr(loop, "_intake_document",
        lambda pr, ed, cfg, tg, doc: intook.setdefault("cap", doc["caption"]))  # or patch intake.intake
    loop.watch(env, tmp_path, _once=True)
    assert intook.get("cap") == "/analyst reproduce"
```
(Implementer: adapt — the cleanest is to factor a `_intake_document(project_root, env_dir, cfg, tg, doc)` helper in loop.py that downloads + parses the caption for a command + calls `intake.intake`, and test THAT is invoked. Match the existing poll_commands block's structure.)
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** In `harn/loop.py`'s watch Telegram block: replace the `poll_commands` call with a single `poll_updates(state_dir)` drain; keep the existing command dispatch over `["commands"]`; add a loop over `["documents"]` calling a new `_intake_document` helper that: `data = tg.download_file(doc["file_id"])`; parse `doc["caption"]` — if it's a `/command`, split into (command, rest) and pass `agent=command`, `text=rest or caption`; else `agent=None`, `text=caption`; call `intake.intake(project_root, env_dir, filename=doc["filename"], data=data, text=text, agent=agent, cfg=cfg)`; reply the outcome via `tg.send`. Guard so a bad document never breaks the tick.
- [ ] **Step 4:** test passes.
- [ ] **Step 5:** full suite (confirm existing watch/triggers tests still pass — the command path behavior is unchanged, just sourced from `poll_updates["commands"]`).
- [ ] **Step 6:** `git add harn/loop.py tests/test_document_intake.py` → commit `feat(loop): route Telegram documents through intake in the watch tick`.

---

### Task 5: HTTP `POST /api/tasks/intake` (multipart, no cgi)

**Files:** Modify `harn/studio.py`; Test `tests/test_document_intake.py`.

**Interfaces:** Produces `intake_payload(project_root, env_dir, *, filename, data, text, agent) -> dict` (thin wrapper over `intake.intake`) and a `do_POST` branch `/api/tasks/intake` that parses a `multipart/form-data` body (fields: `file` required, `text` optional, `agent` optional) WITHOUT `cgi`, then calls it. The route is ALREADY in feature B's `_PROTECTED_ROUTES`, so auth is enforced by the existing gate.

- [ ] **Step 1: failing test** — unit-test the multipart PARSER as a pure function (so no socket needed):
```python
def test_parse_multipart_extracts_file_and_fields():
    from harn import studio
    boundary = "BOUNDARY"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhello\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"agent\"\r\n\r\nanalyst\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"r.pdf\"\r\n"
        f"Content-Type: application/pdf\r\n\r\nPDFBYTES\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    fields, files = studio._parse_multipart(body, f"multipart/form-data; boundary={boundary}")
    assert fields["text"] == "hello" and fields["agent"] == "analyst"
    assert files["file"][0] == "r.pdf"
    assert files["file"][1] == b"PDFBYTES"
```
- [ ] **Step 2:** run → FAIL (no `_parse_multipart`).
- [ ] **Step 3:** Implement `studio._parse_multipart(body: bytes, content_type: str) -> tuple[dict, dict]` using the stdlib `email` module (construct a message with the content-type header + body and walk `.get_payload()`), OR a small manual boundary splitter (split on `--boundary`, parse each part's headers/body). Return `(fields, files)` where `files[name] = (filename, bytes)`. Add `intake_payload` (wrapper over `intake.intake`) and the `do_POST` branch: read the raw body (`Content-Length` bytes), get `Content-Type` header, parse, call `intake_payload`. `file` missing → `{"ok":False,"error":"file is required"}`.
- [ ] **Step 4:** test passes.
- [ ] **Step 5:** full suite.
- [ ] **Step 6:** `git add harn/studio.py tests/test_document_intake.py` → commit `feat(studio): POST /api/tasks/intake (multipart, no cgi) behind the auth gate`.

---

### Task 6: Version bump + verification

- [ ] **Step 1:** Bump `harn/__init__.py` + `pyproject.toml` to the next patch. Commit `chore: bump version`.
- [ ] **Step 2 (manual/browser or curl):** In a scratch project with `harn setup`: `curl -X POST -F 'file=@somefile.txt' -F 'text=investigate this' http://127.0.0.1:PORT/api/tasks/intake?env=...` (loopback, no token) → creates a task with the file attached (verify via `/api/board` and the storage dir). Confirm the task appears with the attachment.
- [ ] **Step 3:** Full suite green. Any bug → fix in the relevant task's files, re-run its tests.
