"""Tests for the Telegram human-in-the-loop (send question + wait for reply).

No network: `harn.telegram._http_post_json` is stubbed to return canned
Telegram API responses, so we exercise offset handling, reply routing,
reminders, and timeout deterministically.
"""
from __future__ import annotations

import harn.telegram as tg
from harn.telegram import TelegramHIL


def _ok(result):
    return {"ok": True, "result": result}


def test_from_env_requires_both(monkeypatch):
    monkeypatch.delenv("HARN_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HARN_TELEGRAM_CHAT_ID", raising=False)
    assert TelegramHIL.from_env() is None

    monkeypatch.setenv("HARN_TELEGRAM_BOT_TOKEN", "t")
    assert TelegramHIL.from_env() is None  # chat id still missing

    monkeypatch.setenv("HARN_TELEGRAM_CHAT_ID", "42")
    hil = TelegramHIL.from_env()
    assert hil is not None and hil.configured()


def test_send_returns_message_id(monkeypatch):
    calls = []

    def fake_post(url, params, timeout):
        calls.append((url, params))
        assert url.endswith("/sendMessage")
        return _ok({"message_id": 555})

    monkeypatch.setattr(tg, "_http_post_json", fake_post)
    hil = TelegramHIL(token="t", chat_id="42")
    assert hil.send("hi") == 555
    assert calls[0][1]["chat_id"] == "42"


def _msg(update_id, chat_id, text, *, reply_to=None, is_bot=False):
    msg = {
        "chat": {"id": chat_id},
        "from": {"is_bot": is_bot},
        "text": text,
    }
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}


def test_wait_for_reply_accepts_plain_text(monkeypatch, tmp_path):
    # getUpdates is called: drain (empty), then a batch with the human reply.
    responses = iter(
        [
            _ok({"message_id": 100}),          # sendMessage (question)
            _ok([_msg(7, 42, "use postgres")]),  # getUpdates -> reply
            _ok({"message_id": 101}),          # sendMessage ("Got it")
        ]
    )
    # _drain runs first via getUpdates(timeout=0); give it an empty result.
    drain = _ok([])

    seq = [drain, *list(responses)]
    it = iter(seq)
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: next(it))

    hil = TelegramHIL(token="t", chat_id="42")
    reply = hil.wait_for_reply("which db?", state_dir=tmp_path, poll_timeout=0)
    assert reply == "use postgres"
    # Offset advanced past update_id 7.
    assert (tmp_path / tg._OFFSET_FILE).read_text().strip() == "8"


def test_wait_for_reply_ignores_bot_and_wrong_chat(monkeypatch, tmp_path):
    batch = _ok(
        [
            _msg(1, 42, "bot noise", is_bot=True),
            _msg(2, 999, "other chat"),
            _msg(3, 42, "the answer"),
        ]
    )
    seq = iter(
        [
            _ok([]),                     # drain
            _ok({"message_id": 100}),    # send question
            batch,                       # getUpdates
            _ok({"message_id": 101}),    # send confirm
        ]
    )
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: next(seq))
    hil = TelegramHIL(token="t", chat_id="42")
    assert hil.wait_for_reply("q", state_dir=tmp_path, poll_timeout=0) == "the answer"


def test_wait_for_reply_reply_to_wrong_message_skipped(monkeypatch, tmp_path):
    # A reply to a different bot message must NOT be taken; then a valid reply.
    seq = iter(
        [
            _ok([]),                                  # drain
            _ok({"message_id": 100}),                 # send question (id 100)
            _ok([_msg(1, 42, "stale", reply_to=99)]),  # reply to wrong msg -> skip
            _ok([_msg(2, 42, "real", reply_to=100)]),  # reply to our msg -> take
            _ok({"message_id": 101}),                 # send confirm
        ]
    )
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: next(seq))
    hil = TelegramHIL(token="t", chat_id="42")
    assert hil.wait_for_reply("q", state_dir=tmp_path, poll_timeout=0) == "real"


def test_wait_for_reply_times_out_and_reminds(monkeypatch, tmp_path):
    # No reply ever arrives. Fake clock crosses remind then timeout thresholds.
    clock = {"t": 1000.0}

    sends = []

    def fake_post(url, params, timeout):
        if url.endswith("/sendMessage"):
            sends.append(params["text"])
            return _ok({"message_id": 1})
        return _ok([])  # getUpdates: nothing

    monkeypatch.setattr(tg, "_http_post_json", fake_post)
    times = iter([1000.0, 1000.0, 1040.0, 1080.0])  # start, loop1, loop2(remind), loop3(timeout)
    hil = TelegramHIL(token="t", chat_id="42")
    reply = hil.wait_for_reply(
        "q",
        state_dir=tmp_path,
        timeout_s=60,
        remind_every_s=30,
        poll_timeout=0,
        _now=lambda: next(times),
    )
    assert reply is None
    # First send is the question; a reminder was sent before timeout.
    assert any("still waiting" in s for s in sends)


def test_wait_for_reply_noop_when_unconfigured(tmp_path):
    hil = TelegramHIL(token="", chat_id="")
    assert hil.wait_for_reply("q", state_dir=tmp_path) is None
