"""External tracker seam (GitLab Issues / Linear / ClickUp) — a minimal
adapter interface with a null implementation. Real providers are separate
future specs (one file each, mirroring how `adapters/` wraps agent CLIs);
this module only defines where the role runner calls through, today as
no-ops (see docs/superpowers/specs/2026-07-18-agent-roles-design.md).
"""
from __future__ import annotations

from . import tasks as tasks_mod


class Tracker:
    """Base interface. The null implementation below is registered by
    default; a provider subclasses this and overrides what it supports."""

    def fetch_task(self, ref: str) -> dict | None:
        raise NotImplementedError

    def push_status(self, task: "tasks_mod.Task") -> None:
        raise NotImplementedError

    def push_result(self, task: "tasks_mod.Task") -> None:
        raise NotImplementedError


class NullTracker(Tracker):
    """No external tracker configured — every call is a no-op."""

    def fetch_task(self, ref: str) -> dict | None:
        return None

    def push_status(self, task: "tasks_mod.Task") -> None:
        return None

    def push_result(self, task: "tasks_mod.Task") -> None:
        return None


_REGISTRY: dict[str, Tracker] = {"null": NullTracker()}


def get(name: str = "null") -> Tracker:
    return _REGISTRY.get(name, _REGISTRY["null"])


def for_task(task: "tasks_mod.Task") -> Tracker:
    """The tracker for `task`'s `external.provider`, or the null tracker for
    a purely local task / an unregistered provider."""
    provider = (task.external or {}).get("provider") if task.external else None
    return get(provider or "null")
