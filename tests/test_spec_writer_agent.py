"""spec-writer default role + workflow preset (docs/superpowers/specs/
2026-07-20-spec-writer-agent-design.md): ships via harn/templates/ so every
new project gets it, discoverable/loadable exactly like a hand-authored
role/workflow."""
from __future__ import annotations

import subprocess

from harn import ENV_DIRNAME, roles, scaffold, workflows


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _scaffolded_env(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    return tmp_path / ENV_DIRNAME


def test_spec_writer_role_discovered(tmp_path):
    env = _scaffolded_env(tmp_path)
    found = [r for r in roles.discover(env) if r.name == "spec-writer"]
    assert len(found) == 1, "spec-writer role not shipped/discovered"
    r = found[0]
    assert r.command == "spec"
    assert r.status == "todo"
    assert r.trigger == "manual"
    assert r.next_status == ""
    assert r.workflow == "spec-writer"
    assert r.oracle is False
    assert r.isolation == "main"
    assert r.push is False
    assert r.body().strip(), "role must have a persona body"


_EXPECTED_STEP_TITLES = [
    "Research",
    "Competitor analysis",
    "Best practices",
    "Risk analysis",
    "Questions to user",
    "Draft spec",
    "Lock spec",
]


def test_spec_writer_workflow_preset_loads(tmp_path):
    env = _scaffolded_env(tmp_path)
    wf = workflows.load(env, "spec-writer")
    assert wf is not None, "spec-writer.json not shipped/loadable"
    assert wf["name"] == "spec-writer"
    steps = [n for n in wf["nodes"] if n.get("kind") == "step"]
    assert [s["title"] for s in steps] == _EXPECTED_STEP_TITLES
    for s in steps:
        assert s["id"], f"step {s['title']!r} missing id"
        assert s["body"].strip(), f"step {s['title']!r} missing body"
    ids = [s["id"] for s in steps]
    assert len(ids) == len(set(ids)), "step ids must be unique"


def test_spec_writer_role_workflow_matches_preset_name(tmp_path):
    env = _scaffolded_env(tmp_path)
    role = next(r for r in roles.discover(env) if r.name == "spec-writer")
    wf = workflows.load(env, role.workflow)
    assert wf is not None, "role's workflow: value must resolve to a real preset"
