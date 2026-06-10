"""Browser verification: app lifecycle, UI verdict, applicability, loop wiring."""
from __future__ import annotations

import http.server
import threading
from pathlib import Path

from harn import browser, design, loop, scaffold, state, tasks, ENV_DIRNAME
from harn.adapters.base import AgentResult
from harn.config import Config
from tests.conftest import make_task


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


# --------------------------------------------------------------------------- #
# loop verdict + applicability
# --------------------------------------------------------------------------- #
def test_ui_verdict():
    assert loop._ui_verdict("all good\nUI: PASS") == "pass"
    assert loop._ui_verdict("UI: FAIL — button missing") == "fail"
    assert loop._ui_verdict("no explicit verdict") == "pass"   # lenient


def test_ui_applicable_via_design_or_skill(tmp_path):
    env = tmp_path / ENV_DIRNAME
    t = make_task(env, "PRJ-001")
    assert not loop._ui_applicable(env, t)
    t2 = make_task(env, "PRJ-002", skills=["ui"])
    assert loop._ui_applicable(env, t2)
    design.save(env, "PRJ-001", "<html></html>")
    assert loop._ui_applicable(env, tasks.find(env, "PRJ-001"))


# --------------------------------------------------------------------------- #
# loop wiring — UI verify runs for tagged tasks and loops on UI: FAIL
# --------------------------------------------------------------------------- #
class ScriptedAdapter:
    """Returns scripted outputs in order (then repeats the last)."""
    name = "fake"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def available(self):
        return True

    def run_turn(self, prompt, cwd):
        self.prompts.append(prompt)
        text = self.outputs.pop(0) if self.outputs else "done"
        return AgentResult(ok=True, text=text)


def _ui_env(tmp_path: Path, url: str) -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    make_task(env, "PRJ-001", title="UI feat", priority=1, skills=["ui"])
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n'
        '[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 6\nverify = false\nplanning = false\n"
        "oracle = false\n"
        f'[browser]\nenabled = true\napp_cmd = ""\napp_url = "{url}"\n'
        "ready_timeout_s = 5\n"
        "[notify]\nwait_for_reply = false\n"
    )
    return env


def test_loop_runs_ui_verify_and_reworks_on_fail(tmp_path, monkeypatch):
    srv, url = _local_http_server()
    try:
        env = _ui_env(tmp_path, url)
        fake = ScriptedAdapter([
            "built the page",          # execution turn 1
            "UI: FAIL — header off",   # ui verify 1 → rework
            "fixed the header",        # execution turn 2
            "UI: PASS",                # ui verify 2 → review
        ])
        monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
        monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

        phase = loop.run(tmp_path, env)

        assert phase == state.REVIEW
        assert tasks.find(env, "PRJ-001").status == tasks.REVIEW
        # ui-verify prompts mention the live URL and the verdict protocol
        ui_prompts = [p for p in fake.prompts if "UI: PASS" in p]
        assert len(ui_prompts) == 2 and url in ui_prompts[0]
        # screenshots dir was prepared for the agent
        assert (env / "state" / "screenshots" / "PRJ-001").is_dir()
    finally:
        srv.shutdown()


def test_loop_skips_ui_verify_when_app_unreachable(tmp_path, monkeypatch):
    env = _ui_env(tmp_path, "http://127.0.0.1:1/")
    fake = ScriptedAdapter(["built the page"])
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    phase = loop.run(tmp_path, env)

    # unreachable app skips the phase but never blocks the pipeline
    assert phase == state.REVIEW
    assert len(fake.prompts) == 1


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
