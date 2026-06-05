"""Chat-grace escalation + cross-channel resolution in `TelegramHIL.await_answer`.

A pending question waits in the chat first; only after the grace does it post to
Telegram. An answer in either channel resolves it, and a chat answer that lands
after the card was posted edits the card.
"""
from __future__ import annotations

import harn.telegram as tg
from harn.telegram import TelegramHIL


def _ok(result):
    return {"ok": True, "result": result}


def _msg(update_id, chat_id, text, *, reply_to=None):
    msg = {"chat": {"id": chat_id}, "from": {"is_bot": False}, "text": text}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}


def test_chat_answer_during_grace_never_posts_to_telegram(monkeypatch, tmp_path):
    posts = []

    def fake_post(url, params, timeout):
        posts.append(url.rsplit("/", 1)[-1])
        return _ok([])  # only the drain getUpdates happens

    monkeypatch.setattr(tg, "_http_post_json", fake_post)
    hil = TelegramHIL(token="t", chat_id="42")

    # local_check flips to True on the 2nd poll — i.e. answered in chat in time.
    seen = {"n": 0}

    def answered_in_chat():
        seen["n"] += 1
        return seen["n"] >= 2

    times = iter([1000.0, 1000.0, 1001.0])  # well inside a 300s grace
    reply, source = hil.await_answer(
        "which db?",
        state_dir=tmp_path,
        pre_grace_s=300,
        local_check=answered_in_chat,
        _now=lambda: next(times),
        _sleep=lambda s: None,
    )
    assert (reply, source) == (None, "chat")
    assert "sendMessage" not in posts  # escalation never fired


def test_escalates_to_telegram_after_grace(monkeypatch, tmp_path):
    seq = iter(
        [
            _ok([]),                              # drain
            _ok({"message_id": 100}),             # sendMessage (card, after grace)
            _ok([_msg(7, 42, "use sqlite", reply_to=100)]),  # telegram reply
            _ok({"message_id": 101}),             # "Got it" confirm
        ]
    )
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: next(seq))
    hil = TelegramHIL(token="t", chat_id="42")

    # grace is 100s; clock jumps past it on the 2nd iteration.
    times = iter([1000.0, 1000.0, 1200.0, 1200.0])
    reply, source = hil.await_answer(
        "which db?",
        state_dir=tmp_path,
        pre_grace_s=100,
        local_check=lambda: False,
        poll_timeout=0,
        _now=lambda: next(times),
        _sleep=lambda s: None,
    )
    assert (reply, source) == ("use sqlite", "telegram")


def test_chat_answer_after_post_edits_card(monkeypatch, tmp_path):
    calls = []

    def fake_post(url, params, timeout):
        method = url.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "sendMessage":
            return _ok({"message_id": 100})
        if method == "editMessageText":
            calls.append(("edit", params.get("message_id"), params.get("text")))
            return _ok({})
        return _ok([])  # getUpdates: nothing

    monkeypatch.setattr(tg, "_http_post_json", fake_post)
    hil = TelegramHIL(token="t", chat_id="42")

    # grace 0 → posts immediately; local answer arrives on the 2nd iteration.
    seen = {"n": 0}

    def answered_in_chat():
        seen["n"] += 1
        return seen["n"] >= 2  # False on iter 1 (so it posts), True on iter 2

    times = iter([1000.0, 1000.0, 1001.0])
    reply, source = hil.await_answer(
        "which db?",
        state_dir=tmp_path,
        pre_grace_s=0,
        local_check=answered_in_chat,
        poll_timeout=0,
        _now=lambda: next(times),
        _sleep=lambda s: None,
    )
    assert (reply, source) == (None, "chat")
    assert "sendMessage" in calls       # card was posted
    edit = [c for c in calls if isinstance(c, tuple) and c[0] == "edit"]
    assert edit and edit[0][1] == 100 and "chat" in edit[0][2].lower()
