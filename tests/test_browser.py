"""Browser dev-server lifecycle + scaffold playwright wiring.

The UI-verify loop stage and its verdict/applicability helpers were removed with
the fixed six-stage pipeline (Task 4)."""
from __future__ import annotations

import http.server
import threading
from pathlib import Path

from harn import browser, scaffold, ENV_DIRNAME


# --------------------------------------------------------------------------- #
# browser.py — dev-server lifecycle
# --------------------------------------------------------------------------- #
def _local_http_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0),
                                 http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/"


def test_wait_ready_against_live_server():
    srv, url = _local_http_server()
    try:
        assert browser.wait_ready(url, timeout_s=5)
    finally:
        srv.shutdown()


def test_wait_ready_times_out_on_closed_port():
    assert not browser.wait_ready("http://127.0.0.1:1/", timeout_s=0)


def test_start_app_without_url_errors():
    app = browser.start_app("", "", Path("."), ready_timeout_s=1)
    assert not app.ready and "app_url" in app.error


def test_start_app_attaches_to_running_server(tmp_path):
    srv, url = _local_http_server()
    try:
        app = browser.start_app("", url, tmp_path, ready_timeout_s=5)
        assert app.ready and app.proc is None
        app.stop()  # no-op for externally managed apps
    finally:
        srv.shutdown()


def test_start_app_bad_command_reports_error(tmp_path):
    app = browser.start_app("definitely-not-a-command-xyz", "http://127.0.0.1:1/",
                            tmp_path, ready_timeout_s=1)
    assert not app.ready and app.error


# Note: `_ui_verdict`, `_ui_applicable`, and the in-loop UI-verify stage were
# removed with the fixed six-stage pipeline (Task 4). UI verification is now an
# ordinary workflow step. Only browser.py's dev-server lifecycle + the scaffold
# playwright wiring are exercised here.


def test_scaffold_adds_playwright_mcp_when_browser_enabled(tmp_path):
    (tmp_path / ENV_DIRNAME).mkdir()
    (tmp_path / ENV_DIRNAME / "harn.toml").write_text(
        "[browser]\nenabled = true\n")
    servers = scaffold._mcp_servers(tmp_path)
    assert "playwright" in servers
    assert "@playwright/mcp@latest" in servers["playwright"]["args"]


def test_scaffold_no_playwright_by_default(tmp_path):
    servers = scaffold._mcp_servers(tmp_path)
    assert "playwright" not in servers
