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
