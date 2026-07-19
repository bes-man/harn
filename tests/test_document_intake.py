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


# --- Task 2: telegram.download_file + poll_documents/poll_updates ---------- #

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


# --- Task 3: intake.intake() -------------------------------------------- #


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


def test_intake_confirm_on_but_no_telegram_defaults_to_run(tmp_path, monkeypatch):
    from harn import intake, scaffold, ENV_DIRNAME
    monkeypatch.delenv("HARN_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HARN_TELEGRAM_CHAT_ID", raising=False)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME  # confirm_before_run defaults True, no credentials file saved
    called = {}
    monkeypatch.setattr(intake.triggers_mod, "dispatch_command",
        lambda pr, ed, cmd, arg, cfg=None: called.setdefault("cmd", cmd) or {"ok": True, "task_id": arg, "status": "done"})
    r = intake.intake(tmp_path, env, filename="f.txt", data=b"x", text="do it", agent="analyst")
    assert r["ok"] is True
    assert called["cmd"] == "analyst"        # unconfigured Telegram → _confirm defaults True → dispatched


# --- watch() integration: a Telegram document routes through intake ------- #

def test_watch_tick_routes_a_telegram_document_to_intake(tmp_path, monkeypatch):
    from harn import loop, scaffold, ENV_DIRNAME, roles
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(exist_ok=True)
    (env / "agents" / "analyst.md").write_text("---\nname: analyst\nstatus: todo\n---\n## Role\na", encoding="utf-8")
    intook = {}

    class FakeHIL:
        def poll_updates(self, sd):
            return {"commands": [], "documents":
                [{"file_id": "F", "filename": "r.pdf", "caption": "/analyst reproduce", "message_id": 1}]}

        def download_file(self, fid):
            return b"BYTES"

        def send(self, *a, **k):
            return 1

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda *_: FakeHIL()))
    monkeypatch.setattr(loop, "_intake_document",
        lambda pr, ed, cfg, tg, doc: intook.setdefault("cap", doc["caption"]))
    loop.watch(env, tmp_path, _once=True)
    assert intook.get("cap") == "/analyst reproduce"


def test_failed_document_download_creates_no_task(tmp_path):
    from harn import loop, scaffold, tasks, ENV_DIRNAME
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(exist_ok=True)
    (env / "agents" / "analyst.md").write_text("---\nname: analyst\nstatus: todo\n---\n## Role\na", encoding="utf-8")
    sent = []

    class FakeHIL:
        def poll_updates(self, sd):
            return {"commands": [], "documents":
                [{"file_id": "F", "filename": "r.pdf", "caption": "/analyst reproduce", "message_id": 1}]}

        def download_file(self, fid):
            return None

        def send(self, msg, *a, **k):
            sent.append(msg)
            return 1

    cfg = Config.load(env)
    doc = {"file_id": "F", "filename": "r.pdf", "caption": "/analyst reproduce", "message_id": 1}
    result = loop._intake_document(tmp_path, env, cfg, FakeHIL(), doc)

    assert result.get("ok") is False
    assert tasks.load_tasks(env) == []
    assert sent and "couldn't download" in sent[0].lower()
