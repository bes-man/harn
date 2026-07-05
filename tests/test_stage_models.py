"""Per-stage model/effort/temperature overrides — provider-agnostic, real
turns only (loop.py's plan/execute/verify/ui_verify/oracle/reconcile), never
the chat-mode WORKFLOW.md steps a human's own IDE session can't be forced to
follow. Config parsing (harn/config.py), the CLI-arg convention every adapter
shares (harn/adapters/base.py), and loop.py actually threading a stage's
override into the right adapter.run_turn call."""
from __future__ import annotations

from pathlib import Path

from harn import loop, tasks, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from harn.config import Config
from .conftest import make_task


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
def test_config_parses_stage_models(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text(
        '[models.plan]\nmodel = "opus"\neffort = "high"\n'
        '[models.execute]\nmodel = "sonnet"\ntemperature = "0.2"\n'
        '[models.oracle]\nmodel = "opus"\n'
    )
    cfg = Config.load(env)
    assert cfg.stage_models == {
        "plan": {"model": "opus", "effort": "high"},
        "execute": {"model": "sonnet", "temperature": "0.2"},
        "oracle": {"model": "opus"},
    }


def test_config_stage_models_empty_by_default(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    assert Config.load(env).stage_models == {}


def test_config_ignores_blank_values(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text('[models.plan]\nmodel = ""\neffort = "high"\n')
    assert Config.load(env).stage_models == {"plan": {"effort": "high"}}


def test_config_ignores_unknown_stage_name(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "harn.toml").write_text('[models.not_a_real_stage]\nmodel = "opus"\n')
    assert Config.load(env).stage_models == {}


# --------------------------------------------------------------------------- #
# loop._stage_overrides
# --------------------------------------------------------------------------- #
def test_stage_overrides_returns_configured_dict():
    cfg = Config(stage_models={"execute": {"model": "sonnet"}})
    assert loop._stage_overrides(cfg, "execute") == {"model": "sonnet"}
    assert loop._stage_overrides(cfg, "verify") == {}


def test_stage_overrides_is_a_copy_not_the_original():
    cfg = Config(stage_models={"execute": {"model": "sonnet"}})
    d = loop._stage_overrides(cfg, "execute")
    d["model"] = "mutated"
    assert cfg.stage_models["execute"]["model"] == "sonnet"


# --------------------------------------------------------------------------- #
# End-to-end: loop.run() actually threads the override into run_turn per stage
# --------------------------------------------------------------------------- #
class RecordingAdapter:
    """Accepts the same kwargs a real adapter's run_turn does and records them
    per call, so a test can assert exactly what each stage received."""
    name = "fake"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        i = min(len(self.calls), len(self.script) - 1)
        self.calls.append({"model": model, "effort": effort, "temperature": temperature})
        item = self.script[i]
        return item(prompt, cwd) if callable(item) else AgentResult(ok=True, text=item)


def _env(tmp_path: Path, extra_toml: str = "") -> Path:
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Feat", priority=1,
              description="## What\nBuild it.\n\n## Done when\n- it works")
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        "[loop]\nmax_iterations = 6\nverify = true\nplanning = false\noracle = false\n"
        "[notify]\nwait_for_reply = false\n" + extra_toml
    )
    return env


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(loop, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])


def test_execute_and_verify_stages_get_their_own_model(tmp_path, monkeypatch):
    env = _env(tmp_path, extra_toml=(
        '[models.execute]\nmodel = "sonnet"\n'
        '[models.verify]\nmodel = "opus"\neffort = "high"\n'
    ))
    fake = RecordingAdapter(["work done", "looks correct\nVERIFY: PASS"])
    _wire(monkeypatch, fake)

    loop.run(tmp_path, env)

    assert fake.calls[0] == {"model": "sonnet", "effort": None, "temperature": None}
    assert fake.calls[1] == {"model": "opus", "effort": "high", "temperature": None}


def test_stage_without_override_passes_no_model(tmp_path, monkeypatch):
    env = _env(tmp_path)   # no [models.*] at all
    fake = RecordingAdapter(["work done", "looks correct\nVERIFY: PASS"])
    _wire(monkeypatch, fake)

    loop.run(tmp_path, env)

    assert all(c == {"model": None, "effort": None, "temperature": None}
              for c in fake.calls)


def test_oracle_stage_gets_its_override(tmp_path, monkeypatch):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    make_task(env, "PRJ-001", title="Feat", priority=1)
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\n'
        "[loop]\nmax_iterations = 8\nverify = false\nplanning = false\noracle = true\n"
        '[notify]\nwait_for_reply = false\n'
        '[models.oracle]\nmodel = "opus"\n'
    )
    fake = RecordingAdapter(["work done", "PASS: looks correct"])
    monkeypatch.setattr(loop, "get_adapter", lambda name: fake)
    monkeypatch.setattr(loop, "_pick_oracle_adapter", lambda cfg: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    loop.run(tmp_path, env)

    # second call is the oracle turn — must carry the oracle-only override
    assert fake.calls[1]["model"] == "opus"
    assert fake.calls[0]["model"] is None   # execute turn had no override
