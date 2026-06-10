"""Browser-verification support: dev-server lifecycle for the Playwright phase.

The UI-verify turn needs the project's app actually running so the agent can
drive it through the Playwright MCP server. harn owns that lifecycle (start →
wait until the URL answers → run the turn → stop), configured per project in
``harn.toml``::

    [browser]
    enabled = true
    app_cmd = "npm run dev"            # empty = app is already running
    app_url = "http://localhost:3000"
    ready_timeout_s = 60

If ``app_cmd`` is empty harn assumes the app is already up at ``app_url`` and
only checks reachability.
"""
from __future__ import annotations

import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AppServer:
    """A started (or externally running) app under test."""
    url: str
    proc: subprocess.Popen | None = None   # None = externally managed
    ready: bool = False
    error: str = ""

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def wait_ready(url: str, timeout_s: int = 60, *, poll_s: float = 1.0,
               _sleep=time.sleep) -> bool:
    """Poll `url` until ANY HTTP response comes back (a 404 still means the
    server is up), or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            urllib.request.urlopen(url, timeout=5)
            return True
        except urllib.error.HTTPError:
            return True            # server answered, just not 2xx — it's up
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        _sleep(poll_s)


def start_app(app_cmd: str, app_url: str, cwd: Path,
              ready_timeout_s: int = 60) -> AppServer:
    """Start the project's app (or attach to an already-running one) and wait
    until `app_url` responds. Never raises — failures land in `.error`."""
    if not app_url.strip():
        return AppServer(url="", error="no [browser] app_url configured")

    proc: subprocess.Popen | None = None
    if app_cmd.strip():
        try:
            proc = subprocess.Popen(
                shlex.split(app_cmd), cwd=str(cwd),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            return AppServer(url=app_url, error=f"could not start app: {exc}")

    app = AppServer(url=app_url, proc=proc)
    app.ready = wait_ready(app_url, ready_timeout_s)
    if not app.ready:
        app.error = f"{app_url} did not respond within {ready_timeout_s}s"
        app.stop()
    return app
