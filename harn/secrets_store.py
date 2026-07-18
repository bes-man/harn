"""One project-level `harn_env/secrets.env` (`KEY=value` lines), gitignored
by scaffold, chmod 600 on write. A `roles.Role` declares the NAMES it needs;
values are injected only into a spawned role run's process environment —
they never enter a prompt, a task file, a transcript, or a Telegram message
(see docs/superpowers/specs/2026-07-18-agent-roles-design.md, "Secrets").
"""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path


def _path(env_dir: Path) -> Path:
    return env_dir / "secrets.env"


def load(env_dir: Path) -> dict[str, str]:
    path = _path(env_dir)
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


def missing(env_dir: Path, names: list[str]) -> list[str]:
    """Declared NAMES with no value in `secrets.env` — the launch refuses to
    start (fail fast, not mid-task) when this is non-empty."""
    if not names:
        return []
    have = load(env_dir)
    return [n for n in names if not have.get(n)]


def ensure_file(env_dir: Path) -> Path:
    """Create an empty, chmod-600 `secrets.env` if missing (scaffold hook)."""
    path = _path(env_dir)
    if not path.exists():
        path.write_text(
            "# KEY=value — one secret per line. Never committed (see .gitignore).\n",
            encoding="utf-8",
        )
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return path


@contextmanager
def injected(env_dir: Path, names: list[str]):
    """Temporarily set the declared secret NAMES in `os.environ` for the
    duration of the `with` block (subprocess.run's default `env=None`
    inherits the current process environment, so a spawned adapter CLI sees
    them) — restored to their prior state on exit, whether or not they were
    set before."""
    values = load(env_dir)
    prior: dict[str, str | None] = {}
    try:
        for name in names:
            if name not in values:
                continue
            prior[name] = os.environ.get(name)
            os.environ[name] = values[name]
        yield
    finally:
        for name, old in prior.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
