"""`harn watch`/`harn ui` must refuse to start a second instance for the
same project -- a real incident: duplicate processes for the same harn_env
(one stale, one started by hand without checking) accumulated over days,
each independently polling/dispatching the same tasks. Neither CLI entry
point had ANY such check before (only the MCP auto-start path did, via
mcp_server._ensure_watch_running) -- these are the first tests for it."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harn import cli, pidlock, scaffold, ENV_DIRNAME


def _args(path, **kw):
    base = {"path": str(path), "poll": 2.0, "host": "127.0.0.1", "port": 8770,
            "no_open": True}
    base.update(kw)
    return SimpleNamespace(**base)


def test_cmd_watch_refuses_if_already_running(tmp_path, capsys):
    scaffold.setup(tmp_path)
    env = _env_dir_of(tmp_path)
    pidlock.claim(env / "state" / "watch.pid", os.getpid())

    with patch("harn.loop.watch") as fake_watch:
        rc = cli.cmd_watch(_args(tmp_path))

    fake_watch.assert_not_called()
    assert rc == 1
    assert "already running" in capsys.readouterr().err


def test_cmd_watch_starts_and_claims_pidfile_when_none_running(tmp_path):
    scaffold.setup(tmp_path)
    env = _env_dir_of(tmp_path)

    with patch("harn.loop.watch") as fake_watch:
        cli.cmd_watch(_args(tmp_path))

    fake_watch.assert_called_once()
    # claimed during the call, released after watch() returns (clean exit)
    assert not (env / "state" / "watch.pid").exists()


def test_cmd_watch_releases_pidfile_on_keyboard_interrupt(tmp_path):
    scaffold.setup(tmp_path)
    env = _env_dir_of(tmp_path)

    with patch("harn.loop.watch", side_effect=KeyboardInterrupt):
        rc = cli.cmd_watch(_args(tmp_path))

    assert rc == 0
    assert not (env / "state" / "watch.pid").exists()


def test_cmd_watch_restarts_over_a_stale_pidfile(tmp_path):
    scaffold.setup(tmp_path)
    env = _env_dir_of(tmp_path)
    pidlock.claim(env / "state" / "watch.pid", 999999999)  # dead pid

    with patch("harn.loop.watch") as fake_watch:
        cli.cmd_watch(_args(tmp_path))

    fake_watch.assert_called_once()


def test_cmd_ui_refuses_if_already_running(tmp_path, capsys):
    scaffold.setup(tmp_path)
    env = _env_dir_of(tmp_path)
    pidlock.claim(env / "state" / "ui.pid", os.getpid())

    with patch("harn.studio.serve") as fake_serve:
        rc = cli.cmd_ui(_args(tmp_path))

    fake_serve.assert_not_called()
    assert rc == 1
    assert "already running" in capsys.readouterr().err


def test_cmd_ui_starts_and_releases_pidfile_after_clean_exit(tmp_path):
    scaffold.setup(tmp_path)
    env = _env_dir_of(tmp_path)

    with patch("harn.studio.serve") as fake_serve:
        cli.cmd_ui(_args(tmp_path))

    fake_serve.assert_called_once()
    assert not (env / "state" / "ui.pid").exists()


def _env_dir_of(project_root: Path) -> Path:
    return project_root / ENV_DIRNAME
