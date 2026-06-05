"""`harn setup` auto-installs enabled code-search backends."""
from __future__ import annotations

from harn import semble_bridge
from harn.config import Config


def test_ensure_backends_skips_when_disabled(monkeypatch):
    cfg = Config(code_search_semble=False, code_search_socraticcode=False)
    calls = []
    monkeypatch.setattr(semble_bridge, "install_semble",
                        lambda: calls.append("semble") or (True, "x"))
    monkeypatch.setattr(semble_bridge, "prepare_socraticode",
                        lambda **k: calls.append("soc") or (True, "x"))
    out = semble_bridge.ensure_backends(cfg)
    assert out == [] and calls == []


def test_ensure_backends_runs_for_enabled(monkeypatch):
    cfg = Config(code_search_semble=True, code_search_socraticcode=True)
    monkeypatch.setattr(semble_bridge, "install_semble", lambda: (True, "installed semble"))
    monkeypatch.setattr(semble_bridge, "prepare_socraticode",
                        lambda **k: (True, "SocratiCode OK"))
    out = semble_bridge.ensure_backends(cfg)
    assert any("semble" in l for l in out)
    assert any("SocratiCode" in l for l in out)
    assert all(l.startswith("✓") for l in out)


def test_install_semble_noop_when_present(monkeypatch):
    monkeypatch.setattr(semble_bridge, "semble_installed", lambda: True)
    monkeypatch.setattr(semble_bridge, "semble_version", lambda: "0.3.2")
    ok, msg = semble_bridge.install_semble()
    assert ok and "already installed" in msg


def test_install_semble_invokes_pip(monkeypatch):
    monkeypatch.setattr(semble_bridge, "semble_installed", lambda: False)
    captured = {}

    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return R()

    monkeypatch.setattr(semble_bridge.subprocess, "run", fake_run)
    ok, msg = semble_bridge.install_semble()
    assert ok
    assert "pip" in captured["cmd"] and "install" in captured["cmd"]
    assert any("semble[mcp]" in c for c in captured["cmd"])


def test_prepare_socraticode_needs_npx(monkeypatch):
    monkeypatch.setattr(semble_bridge.shutil, "which", lambda x: None)
    ok, msg = semble_bridge.prepare_socraticode()
    assert not ok and "npx" in msg


def test_prepare_socraticode_warns_no_docker(monkeypatch):
    monkeypatch.setattr(semble_bridge, "socraticcode_npx_available", lambda: True)
    monkeypatch.setattr(semble_bridge, "_docker_running", lambda: False)

    class R:
        returncode = 0
        stdout = "1.8.13"
        stderr = ""

    monkeypatch.setattr(semble_bridge.subprocess, "run", lambda *a, **k: R())
    ok, msg = semble_bridge.prepare_socraticode()
    assert ok and "Docker" in msg
