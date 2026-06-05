"""Notifications to Telegram and/or Slack, plus an idle-timeout helper.

Configuration via environment variables (preferred for secrets):
  HARN_TELEGRAM_BOT_TOKEN, HARN_TELEGRAM_CHAT_ID
  HARN_SLACK_WEBHOOK_URL
If nothing is configured, notify() is a no-op (returns the list of channels hit).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request


def _post(url: str, payload: dict, timeout: int = 10) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def notify(text: str, *, skip_telegram: bool = False) -> list[str]:
    """Send `text` to every configured channel. Returns channels reached.

    `skip_telegram` lets the caller suppress the plain Telegram push when an
    interactive Telegram card (telegram.TelegramHIL) is being sent instead, to
    avoid a duplicate message.
    """
    reached: list[str] = []

    token = os.environ.get("HARN_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("HARN_TELEGRAM_CHAT_ID")
    if token and chat_id and not skip_telegram:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        if _post(url, {"chat_id": chat_id, "text": text}):
            reached.append("telegram")

    webhook = os.environ.get("HARN_SLACK_WEBHOOK_URL")
    if webhook:
        if _post(webhook, {"text": text}):
            reached.append("slack")

    return reached


def seconds_blocked(blocked_since: float | None) -> float:
    if not blocked_since:
        return 0.0
    return max(0.0, time.time() - blocked_since)


def should_remind(blocked_since: float | None, idle_minutes: int) -> bool:
    return seconds_blocked(blocked_since) >= idle_minutes * 60
