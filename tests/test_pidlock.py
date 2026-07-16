"""pidlock: the shared PID-file singleton-lock pattern every long-lived harn
background process (watch, ui, an MCP-launched run) uses so a second instance
never races the first one against the same harn_env.

This started as three independent copy-pasted implementations (watch.pid in
mcp_server.py, ui_run.pid in runner.py, ui_mcp.pid in studio.py) before a real
incident: duplicate `harn watch`/`harn ui` processes for the same project
accumulated over days, each independently polling/dispatching the same tasks.
Consolidated here."""
from __future__ import annotations

import os

from harn import pidlock


def test_read_alive_pid_none_when_file_missing(tmp_path):
    assert pidlock.read_alive_pid(tmp_path / "nope.pid") is None


def test_read_alive_pid_returns_pid_of_a_live_process(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text(str(os.getpid()))
    assert pidlock.read_alive_pid(p) == os.getpid()


def test_read_alive_pid_cleans_up_a_stale_file(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text("999999999")  # not a real pid
    assert pidlock.read_alive_pid(p) is None
    assert not p.exists()


def test_read_alive_pid_cleans_up_a_corrupt_file(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text("not-a-number")
    assert pidlock.read_alive_pid(p) is None
    assert not p.exists()


def test_claim_writes_own_pid_by_default(tmp_path):
    p = tmp_path / "sub" / "x.pid"
    pidlock.claim(p)
    assert p.read_text() == str(os.getpid())


def test_claim_writes_a_given_pid(tmp_path):
    p = tmp_path / "x.pid"
    pidlock.claim(p, pid=4242)
    assert p.read_text() == "4242"


def test_release_removes_the_file(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text("123")
    pidlock.release(p)
    assert not p.exists()


def test_release_is_a_noop_if_already_gone(tmp_path):
    pidlock.release(tmp_path / "nope.pid")  # must not raise


def test_read_alive_json_none_when_file_missing(tmp_path):
    assert pidlock.read_alive_json(tmp_path / "nope.json") is None


def test_read_alive_json_returns_data_for_a_live_process(tmp_path):
    import json
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"pid": os.getpid(), "port": 9999}))
    assert pidlock.read_alive_json(p) == {"pid": os.getpid(), "port": 9999}


def test_read_alive_json_cleans_up_a_dead_pid(tmp_path):
    import json
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"pid": 999999999, "port": 9999}))
    assert pidlock.read_alive_json(p) is None
    assert not p.exists()


def test_read_alive_json_cleans_up_corrupt_json(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("not json")
    assert pidlock.read_alive_json(p) is None
    assert not p.exists()


def test_read_alive_json_cleans_up_missing_pid_key(tmp_path):
    import json
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"port": 9999}))
    assert pidlock.read_alive_json(p) is None
    assert not p.exists()


def test_claim_json_writes_the_given_data(tmp_path):
    p = tmp_path / "sub" / "x.json"
    pidlock.claim_json(p, {"pid": 4242, "port": 9999})
    import json
    assert json.loads(p.read_text()) == {"pid": 4242, "port": 9999}
