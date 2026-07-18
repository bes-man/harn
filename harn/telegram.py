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
import ssl
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_OFFSET_FILE = ".telegram_offset"
_GRACE_POLL_SEC = 2  # how often to check the local channel during chat grace
_AUTO_CALLBACK = "harn_auto"  # callback_data for the "Decide for me" button
_CREDENTIALS_FILE = "telegram_credentials.json"
_warned: set[str] = set()


def _credentials_path(env_dir: Path) -> Path:
    return env_dir / "state" / _CREDENTIALS_FILE


def load_credentials(env_dir: Path | None = None) -> tuple[str, str]:
    """Load Telegram credentials, preferring process environment overrides."""
    token = (os.environ.get("HARN_TELEGRAM_BOT_TOKEN") or "").strip()
    user_id = (os.environ.get("HARN_TELEGRAM_CHAT_ID") or "").strip()
    if env_dir is not None and (not token or not user_id):
        try:
            saved = json.loads(_credentials_path(env_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            saved = {}
        token = token or str(saved.get("api_key") or "").strip()
        user_id = user_id or str(saved.get("user_id") or "").strip()
    return token, user_id


def save_credentials(env_dir: Path, api_key: str, user_id: str) -> None:
    """Persist local Studio credentials with owner-only file permissions.

    Blank values keep their existing counterpart, so saving unrelated Settings
    never erases a token that the UI intentionally does not read back.
    """
    old_key, old_user = load_credentials(env_dir)
    data = {
        "api_key": (api_key or "").strip() or old_key,
        "user_id": (user_id or "").strip() or old_user,
    }
    path = _credentials_path(env_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if env_dir.name == "harn_env":
        ignore = env_dir.parent / ".gitignore"
        entry = "harn_env/state/telegram_credentials.json"
        existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        if entry not in existing.splitlines():
            prefix = existing.rstrip("\n")
            ignore.write_text(
                (prefix + "\n" if prefix else "") + entry + "\n",
                encoding="utf-8")


def _format_card(task_id: str | None, emoji: str, status: str,
                 question: str, footer: str = "") -> str:
    """Render a question card: id + status header, the question, then a footer
    (instructions while waiting, or the answer once resolved)."""
    head = f"{emoji} {task_id}" if task_id else f"{emoji} harn needs your input"
    parts = [head, f"Status: {status}", "", question]
    if footer:
        parts += ["", footer]
    return "\n".join(parts)


def _warn_once(key: str, message: str) -> None:
    """Print a warning once per process so a recurring failure isn't silent."""
    if key not in _warned:
        _warned.add(key)
        print(f"[harn] Telegram: {message}", file=sys.stderr)


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context that works across machines.

    Uses certifi's CA bundle when available (fixes the common macOS / corporate
    'certificate verify failed' issue). Set HARN_TELEGRAM_SSL_VERIFY=0 to disable
    verification entirely (last resort, e.g. behind a TLS-intercepting proxy).
    """
    if os.environ.get("HARN_TELEGRAM_SSL_VERIFY") == "0":
        _warn_once("ssl_off", "TLS verification disabled (HARN_TELEGRAM_SSL_VERIFY=0)")
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_post_json(url: str, params: dict, timeout: int) -> dict | None:
    """POST form-encoded params; return parsed JSON, or None on failure.

    Isolated at module level so tests can stub the network without monkeypatching
    urllib internals. Logs once on network/TLS errors so failures aren't silent.
    """
    encoded: dict[str, str] = {}
    for key, val in params.items():
        if val is None:
            continue
        encoded[key] = json.dumps(val) if isinstance(val, (dict, list)) else str(val)
    data = urllib.parse.urlencode(encoded).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except ssl.SSLError as e:
        _warn_once("ssl", f"TLS error ({e}). If behind a proxy/AV, install certifi "
                          "or set HARN_TELEGRAM_SSL_VERIFY=0.")
        return None
    except Exception as e:
        _warn_once("net", f"request to {url.rsplit('/', 1)[-1]} failed: {e}")
        return None
    return body if isinstance(body, dict) else None


@dataclass
class TelegramHIL:
    token: str
    chat_id: str

    # --- construction ---
    @classmethod
    def from_env(cls, env_dir: Path | None = None) -> "TelegramHIL | None":
        token, chat_id = load_credentials(env_dir)
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

    def send(self, text: str, *, auto_button: bool = False) -> int | None:
        """Send a message to the configured chat. Returns its message_id.

        With `auto_button=True`, attach a "🤖 Decide for me" inline button so the
        human can delegate the decision back to the agent.
        """
        params = {"chat_id": self.chat_id, "text": text}
        if auto_button:
            params["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": "🤖 Decide for me", "callback_data": _AUTO_CALLBACK}]
                ]
            }
        body = self._api("sendMessage", params)
        if not body:
            return None
        try:
            return int(body["result"]["message_id"])
        except (KeyError, TypeError, ValueError):
            return None

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        self._api("answerCallbackQuery",
                  {"callback_query_id": callback_id, "text": text})

    def edit_message(self, message_id: int, text: str, *,
                     remove_buttons: bool = False) -> bool:
        """Edit a previously sent message (e.g. update a card's status/answer).

        With `remove_buttons=True`, also strip the inline keyboard (an empty
        keyboard removes it in the Bot API)."""
        params = {"chat_id": self.chat_id, "message_id": message_id, "text": text}
        if remove_buttons:
            params["reply_markup"] = {"inline_keyboard": []}
        body = self._api("editMessageText", params)
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
                "allowed_updates": ["message", "callback_query"],
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

    def _auto_pressed(self, updates: list[dict]) -> str | None:
        """Return the callback_query id if the human pressed 'Decide for me'."""
        for upd in updates:
            cq = upd.get("callback_query") or {}
            if cq.get("data") == _AUTO_CALLBACK:
                msg = cq.get("message") or {}
                if str(msg.get("chat", {}).get("id")) == str(self.chat_id):
                    return str(cq.get("id", ""))
        return None

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

    def poll_commands(self, state_dir: Path) -> list[dict]:
        """Non-blocking drain of new updates (timeout=0), returning every
        `/command …` text message from the trusted chat (see the chat_id
        check in `_reply_from_updates` — same trust boundary, same offset
        file, so this and `await_answer` never reprocess each other's
        updates). Used by `harn watch`'s per-tick trigger scan (agent-
        triggers spec) — commands from any other chat are silently ignored,
        and non-command text (HIL answers) is left for `await_answer`.
        """
        offset = self._load_offset(state_dir)
        offset, updates = self._get_updates(offset, poll_timeout=0)
        self._save_offset(state_dir, offset)
        out: list[dict] = []
        for upd in updates:
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                continue
            if (msg.get("from") or {}).get("is_bot"):
                continue
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            out.append({"text": text, "message_id": msg.get("message_id")})
        return out

    def await_answer(
        self,
        question: str,
        *,
        state_dir: Path,
        task_id: str | None = None,
        timeout_s: int = 0,
        remind_every_s: int = 0,
        poll_timeout: int = 30,
        pre_grace_s: int = 0,
        local_check=None,
        auto_button: bool = True,
        _now=time.time,
        _sleep=time.sleep,
    ) -> tuple[str | None, str]:
        """Wait for the human's answer across two channels. Returns
        `(reply, source)` where source is:
          'telegram' — the human replied in Telegram (reply is the text);
          'chat'     — the question was answered locally (chat/CLI) while waiting,
                       so it's already recorded elsewhere (reply is None);
          'auto'     — the human pressed 'Decide for me'; the agent should choose;
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

        def _resolve(emoji: str, status: str, footer: str) -> None:
            """Edit the card in place: new status, drop the buttons, show outcome."""
            if posted and question_msg_id is not None:
                self.edit_message(
                    question_msg_id,
                    _format_card(task_id, emoji, status, question, footer),
                    remove_buttons=True,
                )

        while True:
            # 1) chat / CLI channel — answered locally?
            if local_check is not None and local_check():
                _resolve("✅", "answered in chat", "💬 Answered in chat.")
                return (None, "chat")

            now = _now()
            # 2) escalate to Telegram once the chat grace has elapsed
            if not posted and now - start >= pre_grace_s:
                footer = "Reply here to unblock."
                if auto_button:
                    footer += " Or tap 🤖 Decide for me to let the agent choose."
                question_msg_id = self.send(
                    _format_card(task_id, "🟡", "awaiting your reply",
                                 question, footer),
                    auto_button=auto_button,
                )
                posted = True
                last_remind = now

            # 3) Telegram channel — only poll once the card is up
            if posted:
                offset, updates = self._get_updates(offset, poll_timeout)
                self._save_offset(state_dir, offset)
                cb_id = self._auto_pressed(updates)
                if cb_id:
                    self._answer_callback(cb_id, "Agent will decide.")
                    _resolve("🤖", "agent deciding",
                             "🤖 You chose “Decide for me” — the agent will pick "
                             "the best option and proceed.")
                    return (None, "auto")
                reply = self._reply_from_updates(updates, question_msg_id)
                if reply is not None:
                    _resolve("✅", "answered", f"💬 {reply}")
                    return (reply, "telegram")
                if remind_every_s and now - last_remind >= remind_every_s:
                    self.send("⏳ Still waiting on your answer for "
                              f"{task_id or 'a question'}…")
                    last_remind = now
            else:
                _sleep(min(poll_timeout, _GRACE_POLL_SEC))

            if timeout_s and now - start >= timeout_s:
                _resolve("⌛", "timed out", "No reply — falling back to the CLI.")
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
