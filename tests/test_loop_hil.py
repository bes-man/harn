"""The loop's HIL wait integration point (`_await_answer`)."""
from __future__ import annotations

from harn import loop
from harn.config import Config


def test_await_disabled_returns_empty(tmp_path):
    cfg = Config(wait_for_reply=False)
    assert loop._await_answer(tmp_path, cfg, "task1", "q?") == (None, "")


def test_await_unconfigured_returns_empty(monkeypatch, tmp_path):
    cfg = Config(wait_for_reply=True)
    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda: None))
    assert loop._await_answer(tmp_path, cfg, "task1", "q?") == (None, "")


def test_await_chat_channel_skips_telegram(monkeypatch, tmp_path):
    cfg = Config(wait_for_reply=True, hil_channel="chat")
    # Even if Telegram is configured, the chat-only channel never posts.
    monkeypatch.setattr(loop.TelegramHIL, "from_env",
                        staticmethod(lambda: object()))
    assert loop._await_answer(tmp_path, cfg, "task1", "q?") == (None, "")


def test_await_both_passes_chat_grace_and_local_check(monkeypatch, tmp_path):
    cfg = Config(wait_for_reply=True, hil_channel="both", chat_grace_minutes=5,
                 idle_minutes=5, wait_timeout_minutes=10)
    (tmp_path / "state").mkdir()
    captured = {}

    class FakeHIL:
        def await_answer(self, question, *, state_dir, timeout_s, remind_every_s,
                         pre_grace_s, local_check):
            captured.update(question=question, timeout_s=timeout_s,
                            remind_every_s=remind_every_s, pre_grace_s=pre_grace_s,
                            local_check=local_check)
            return ("use sqlite", "telegram")

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda: FakeHIL()))
    reply, source = loop._await_answer(tmp_path, cfg, "task1", "which db?")
    assert (reply, source) == ("use sqlite", "telegram")
    assert captured["question"] == "which db?"
    assert captured["timeout_s"] == 600          # 10 min
    assert captured["remind_every_s"] == 300     # 5 min
    assert captured["pre_grace_s"] == 300        # 5 min chat grace
    assert callable(captured["local_check"])


def test_await_telegram_channel_has_no_grace(monkeypatch, tmp_path):
    cfg = Config(wait_for_reply=True, hil_channel="telegram", chat_grace_minutes=5)
    (tmp_path / "state").mkdir()
    captured = {}

    class FakeHIL:
        def await_answer(self, question, *, state_dir, timeout_s, remind_every_s,
                         pre_grace_s, local_check):
            captured["pre_grace_s"] = pre_grace_s
            return (None, "")

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda: FakeHIL()))
    loop._await_answer(tmp_path, cfg, "task1", "q?")
    assert captured["pre_grace_s"] == 0  # telegram channel escalates immediately
