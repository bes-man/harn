"""Task id helpers.

Without Jira: ids look like ``PRJ-001``, ``PRJ-002`` … (prefix from
``[harn] project`` in harn.toml, counter auto-incremented by
``tasks.next_id()``).

With Jira (or another tracker): use the tracker key directly as the filename,
e.g. ``AUTH-42.json``.  ``is_tracker_key()`` recognises the pattern.
"""
from __future__ import annotations

import re

# Matches "ALPHA-digits" — covers Jira, Linear, GitHub-style keys.
_TRACKER_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def is_tracker_key(s: str) -> bool:
    """True when ``s`` looks like a tracker key (e.g. AUTH-42, PRJ-001)."""
    return bool(_TRACKER_KEY_RE.match(s.strip().upper()))
