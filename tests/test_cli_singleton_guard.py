"""`harn watch` must refuse to start a second instance for the same project
-- a real incident: duplicate processes for the same harn_env (one stale,
one started by hand without checking) accumulated over days, each
independently polling/dispatching the same tasks.

`harn ui` is different: Studio already serves every project from ONE HTTP
process via `?env=<path>`, so a second `harn ui` invocation (for the same
project OR a different one) should just point the browser at the ALREADY
running instance instead of trying to start a redundant second server --
that's what a real user asked for after hitting a raw EADDRINUSE traceback
starting `harn ui` for project B while project A's was still listening on
the same default port."""
from __future__ import annotations

import json
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


def _marker(tmp_path, monkeypatch):
    """Redirect cli's global UI marker to a scratch path so tests never
    touch the real ~/.harn/ui.json."""
    m = tmp_path / "global_ui.json"
    monkeypatch.setattr(cli, "_global_ui_marker", lambda: m)
    return m


def test_cmd_ui_opens_existing_instance_instead_of_a_new_one(tmp_path, monkeypatch, capsys):
    """The real-world case: project A's `harn ui` is already running (on
    ANY port); starting `harn ui` for project B must open a browser tab at
    the EXISTING instance with ?env=<project B's env_dir> instead of trying
    (and failing, or redundantly succeeding) to bind a second server."""
    scaffold.setup(tmp_path)
    env = _env_dir_of(tmp_path)
    marker = _marker(tmp_path, monkeypatch)
    marker.write_text(json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": 9999}))

    with patch("harn.studio.serve") as fake_serve, \
         patch("webbrowser.open") as fake_open:
        rc = cli.cmd_ui(_args(tmp_path, no_open=False))

    fake_serve.assert_not_called()
    assert rc == 0
    fake_open.assert_called_once()
    (opened_url,), _ = fake_open.call_args
    assert opened_url.startswith("http://127.0.0.1:9999/?env=")
    assert str(env) in opened_url
    assert "already running" in capsys.readouterr().out


def test_cmd_ui_respects_no_open_when_reusing_existing_instance(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    marker = _marker(tmp_path, monkeypatch)
    marker.write_text(json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": 9999}))

    with patch("harn.studio.serve") as fake_serve, \
         patch("webbrowser.open") as fake_open:
        cli.cmd_ui(_args(tmp_path, no_open=True))

    fake_serve.assert_not_called()
    fake_open.assert_not_called()


def test_cmd_ui_starts_and_releases_global_marker_after_clean_exit(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    marker = _marker(tmp_path, monkeypatch)

    with patch("harn.studio.serve") as fake_serve:
        cli.cmd_ui(_args(tmp_path))

    fake_serve.assert_called_once()
    assert not marker.exists()


def test_cmd_ui_restarts_over_a_stale_global_marker(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    marker = _marker(tmp_path, monkeypatch)
    marker.write_text(json.dumps({"pid": 999999999, "host": "127.0.0.1", "port": 9999}))

    with patch("harn.studio.serve") as fake_serve, \
         patch("webbrowser.open") as fake_open:
        cli.cmd_ui(_args(tmp_path))

    fake_serve.assert_called_once()
    fake_open.assert_not_called()


def test_cmd_ui_reports_a_friendly_error_when_the_port_is_taken(tmp_path, monkeypatch, capsys):
    """A port collision that isn't tracked by our own marker at all (e.g. a
    totally unrelated process) must exit(1) with a clear message pointing at
    --port, not crash with a raw OSError traceback."""
    scaffold.setup(tmp_path)
    marker = _marker(tmp_path, monkeypatch)

    with patch("harn.studio.serve",
               side_effect=OSError(48, "Address already in use")):
        rc = cli.cmd_ui(_args(tmp_path))

    assert rc == 1
    err = capsys.readouterr().err
    assert "already in use" in err
    assert "--port" in err
    assert not marker.exists()  # released, not left dangling


def _env_dir_of(project_root: Path) -> Path:
    return project_root / ENV_DIRNAME
