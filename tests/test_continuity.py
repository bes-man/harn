"""Lightweight continuity (variant 2): scratchpad + decisions in task JSON.

The executor carries a short note + its decisions between iterations (cheap
continuity). The oracle treats those decisions as CLAIMS to verify, not as part
of the task's requirements.
"""
from __future__ import annotations

from pathlib import Path

from harn import loop, tasks
from .conftest import make_task


def _env(tmp_path: Path) -> Path:
    env = tmp_path / "harn_env"
    env.mkdir()
    return env


# --- persistence ---------------------------------------------------------- #

def test_scratchpad_round_trips(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.set_scratchpad(t, "Done: schema. Left: indexes. Gotcha: tz in created_at.")
    fresh = tasks.find(env, "PRJ-001")
    assert "Left: indexes" in fresh.scratchpad


def test_set_scratchpad_replaces(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.set_scratchpad(t, "first")
    tasks.set_scratchpad(t, "second")
    assert tasks.find(env, "PRJ-001").scratchpad == "second"


def test_record_decision_appends(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.record_decision(t, "Use sqlite", "no network dep", agent="claude")
    tasks.record_decision(t, "15m token expiry", "security default")
    fresh = tasks.find(env, "PRJ-001")
    assert len(fresh.decisions) == 2
    assert fresh.decisions[0].decision == "Use sqlite"
    assert fresh.decisions[0].rationale == "no network dep"
    assert fresh.decisions[0].agent == "claude"
    assert fresh.decisions[0].ts  # timestamped


def test_empty_decision_is_ignored(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.record_decision(t, "   ")
    assert tasks.find(env, "PRJ-001").decisions == []


def test_accept_clears_scratchpad_but_keeps_decisions(tmp_path):
    env = _env(tmp_path)
    t = make_task(env, "PRJ-001")
    tasks.set_scratchpad(t, "work in progress note")
    tasks.record_decision(t, "Use sqlite", "no network dep")
    tasks.submit_for_review(t, "claude")
    tasks.accept(t, notes="ship it")

    fresh = tasks.find(env, "PRJ-001")
    assert fresh.scratchpad == ""          # transient note cleared
    assert len(fresh.decisions) == 1       # decisions are kept as history


# --- prompt injection ----------------------------------------------------- #

def test_continuity_block_in_executor_prompt(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n')
    t = make_task(env, "PRJ-001")
    tasks.set_scratchpad(t, "REMEMBER: indexes pending")
    tasks.record_decision(t, "Use sqlite", "no network dep")
    t = tasks.find(env, "PRJ-001")

    cfg = loop.Config.load(env)
    prompt = loop._build_prompt(env, cfg, t)
    assert "REMEMBER: indexes pending" in prompt
    assert "Use sqlite" in prompt
    assert "Decisions you've already made" in prompt


def test_oracle_sees_decisions_as_claims_not_requirements(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n')
    t = make_task(env, "PRJ-001")
    tasks.record_decision(t, "Use sqlite", "no network dep")
    t = tasks.find(env, "PRJ-001")

    cfg = loop.Config.load(env)
    prompt = loop._build_oracle_prompt(env, cfg, t, diff="some diff")

    # The decision appears, but framed as a claim to verify — not a spec.
    assert "Use sqlite" in prompt
    assert "VERIFY each" in prompt
    assert "do not assume" in prompt.lower()
    # The scratchpad (working state) must NOT leak into the oracle as a requirement
    tasks.set_scratchpad(tasks.find(env, "PRJ-001"), "SECRET WORKING NOTE")
    t2 = tasks.find(env, "PRJ-001")
    oracle_prompt = loop._build_oracle_prompt(env, cfg, t2, diff="d")
    assert "SECRET WORKING NOTE" not in oracle_prompt


def test_scratchpad_absent_means_no_block(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n')
    t = make_task(env, "PRJ-001")
    cfg = loop.Config.load(env)
    prompt = loop._build_prompt(env, cfg, t)
    assert "continuity" not in prompt.lower()
    assert "Decisions you've already made" not in prompt
