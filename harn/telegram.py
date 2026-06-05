"""Telegram human-in-the-loop: send the blocking question AND wait for the reply.

`notify.py` only *pushes* a one-way message; the human then has to run
`harn answer "..."` from a terminal. This module closes that loop: when the
agent blocks, harn can post the question to Telegram and long-poll for the
human's reply right there, then resume automatically.

Pure stdlib (urllib) on purpose — same dependency-free footprint as notify.py,
no python-telegram-bot. Reuses the existing secrets:
  HARN_TELEGRAM_BOT_TOKEN, HARN_TELEGRAM_CHAT_ID

The reply is taken from either a native reply to the bot's question or, in the
common single-chat case, the next plain text message the human sends.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_OFFSET_FILE = ".telegram_offset"
_GRACE_POLL_SEC = 2  # how often to check the local channel during chat grace


def _http_post_json(url: str, params: dict, timeout: int) -> dict | None:
    """POST form-encoded params; return parsed JSON, or None on any failure.

    Isolated at module level so tests can stub the network without monkeypatching
    urllib internals.
    """
    encoded: dict[str, str] = {}
    for key, val in params.items():
        if val is None:
            continue
        encoded[key] = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
    data = urllib.parse.urlencode(encoded).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@dataclass
class TelegramHIL:
    token: str
    chat_id: str

    # --- construction ---
    @classmethod
    def from_env(cls) -> "TelegramHIL | None":
        token = (os.environ.get("HARN_TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.environ.get("HARN_TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat_id:
            return None
        return cls(token=token, chat_id=chat_id)

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    # --- low-level API ---
    def _api(self, method: str, params: dict, timeout: int = 15) -> dict | None:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = _http_post_json(url, params, timeout)
        if body is None or not body.get("ok"):
            return None
        return body

    def send(self, text: str) -> int | None:
        """Send a message to the configured chat. Returns its message_id."""
        body = self._api("sendMessage", {"chat_id": self.chat_id, "text": text})
        if not body:
            return None
        try:
            return int(body["result"]["message_id"])
        except (KeyError, TypeError, ValueError):
            return None

    def edit_message(self, message_id: int, text: str) -> bool:
        """Edit a previously sent message (e.g. mark a card as answered)."""
        body = self._api(
            "editMessageText",
            {"chat_id": self.chat_id, "message_id": message_id, "text": text},
        )
        return body is not None

    # --- offset persistence (don't reprocess old updates across runs) ---
    @staticmethod
    def _offset_path(state_dir: Path) -> Path:
        return state_dir / _OFFSET_FILE

    def _load_offset(self, state_dir: Path) -> int:
        p = self._offset_path(state_dir)
        if p.exists():
            try:
                return int(p.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                return 0
        return 0

    def _save_offset(self, state_dir: Path, offset: int) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self._offset_path(state_dir).write_text(str(offset), encoding="utf-8")

    # --- polling ---
    def _get_updates(self, offset: int, poll_timeout: int) -> tuple[int, list[dict]]:
        body = self._api(
            "getUpdates",
            {
                "offset": offset,
                "timeout": poll_timeout,
                "allowed_updates": ["message"],
            },
            timeout=poll_timeout + 10,
        )
        if not body:
            return offset, []
        updates = body.get("result") or []
        next_offset = offset
        for upd in updates:
            next_offset = max(next_offset, int(upd["update_id"]) + 1)
        return next_offset, updates

    def _drain(self, offset: int) -> int:
        """Fast-forward past any backlog so a stale message isn't read as the
        answer. Uses timeout=0 (returns immediately)."""
        offset, _ = self._get_updates(offset, poll_timeout=0)
        return offset

    def _reply_from_updates(
        self, updates: list[dict], question_msg_id: int | None
    ) -> str | None:
        """Pull the human's answer text out of a batch of updates."""
        for upd in updates:
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                continue
            if (msg.get("from") or {}).get("is_bot"):
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            reply_to = msg.get("reply_to_message") or {}
            # Prefer a native reply to our question; otherwise accept any text
            # (single-chat case). A reply to a *different* bot message is skipped.
            if reply_to:
                if question_msg_id and int(reply_to.get("message_id", 0)) != question_msg_id:
                    continue
            return text
        return None

    def await_answer(
        self,
        question: str,
        *,
        state_dir: Path,
        timeout_s: int = 0,
        remind_every_s: int = 0,
        poll_timeout: int = 30,
        pre_grace_s: int = 0,
        local_check=None,
        _now=time.time,
        _sleep=time.sleep,
    ) -> tuple[str | None, str]:
        """Wait for the human's answer across two channels. Returns
        `(reply, source)` where source is:
          'telegram' — the human replied in Telegram (reply is the text);
          'chat'     — the question was answered locally (chat/CLI) while waiting,
                       so it's already recorded elsewhere (reply is None);
          ''         — timeout, or Telegram not configured.

        `pre_grace_s` holds off posting to Telegram that long, polling
        `local_check()` first — this is the "answer in chat, escalate later"
        behavior. `local_check` returns truthy once the question is answered
        locally. If the card was already posted when that happens, it's edited
        to show the answer landed in chat.
        """
        if not self.configured():
            return (None, "")
        offset = self._drain(self._load_offset(state_dir))
        self._save_offset(state_dir, offset)

        start = _now()
        last_remind = start
        posted = False
        question_msg_id: int | None = None

        while True:
            # 1) chat / CLI channel — answered locally?
            if local_check is not None and local_check():
                if posted and question_msg_id is not None:
                    self.edit_message(
                        question_msg_id, "✅ Answered in chat — resuming."
                    )
                return (None, "chat")

            now = _now()
            # 2) escalate to Telegram once the chat grace has elapsed
            if not posted and now - start >= pre_grace_s:
                question_msg_id = self.send(
                    "🟡 harn needs your input:\n\n"
                    f"{question}\n\n"
                    "Reply to this message (or just send your answer) to unblock."
                )
                posted = True
                last_remind = now

            # 3) Telegram channel — only poll once the card is up
            if posted:
                offset, updates = self._get_updates(offset, poll_timeout)
                self._save_offset(state_dir, offset)
                reply = self._reply_from_updates(updates, question_msg_id)
                if reply is not None:
                    self.send("✅ Got it — resuming.")
                    return (reply, "telegram")
                if remind_every_s and now - last_remind >= remind_every_s:
                    self.send("⏳ harn is still waiting on your answer:\n\n" + question)
                    last_remind = now
            else:
                _sleep(min(poll_timeout, _GRACE_POLL_SEC))

            if timeout_s and now - start >= timeout_s:
                return (None, "")

    def wait_for_reply(
        self,
        question: str,
        *,
        state_dir: Path,
        timeout_s: int = 0,
        remind_every_s: int = 0,
        poll_timeout: int = 30,
        _now=time.time,
    ) -> str | None:
        """Back-compat: post `question` and block for a Telegram reply only.

        Equivalent to `await_answer` with no chat grace and no local channel.
        """
        reply, _ = self.await_answer(
            question,
            state_dir=state_dir,
            timeout_s=timeout_s,
            remind_every_s=remind_every_s,
            poll_timeout=poll_timeout,
            _now=_now,
        )
        return reply
