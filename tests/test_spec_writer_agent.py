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
