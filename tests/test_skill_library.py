"""Skill gap detection + library bootstrap."""
from __future__ import annotations

from pathlib import Path

from harn import skill_library, skills, ENV_DIRNAME
from tests.conftest import make_task


def _env(tmp_path) -> Path:
    env = tmp_path / ENV_DIRNAME
    (env / "skills").mkdir(parents=True)
    return env


def test_infer_domains_from_text():
    assert "frontend" in skill_library.infer_domains("build a responsive button component")
    assert "security" in skill_library.infer_domains("add login with password hashing")
    assert "testing" in skill_library.infer_domains("write vitest unit tests")
    # Russian keywords
    assert "frontend" in skill_library.infer_domains("сверстать форму на фронте")


def test_gap_detected_when_no_skill(tmp_path):
    env = _env(tmp_path)
    task = make_task(env, "T-1", title="Build the onboarding form",
                     description="A responsive multi-step UI form")
    missing = skill_library.gaps(env, task)
    names = {m.name for m in missing}
    assert "frontend" in names


def test_no_gap_when_alias_skill_exists(tmp_path):
    env = _env(tmp_path)
    # An existing substantive 'ui' skill should satisfy the 'frontend' domain.
    skill_library.install(env, "frontend")  # writes a 'frontend' skill
    task = make_task(env, "T-2", title="Build a UI component")
    missing = {m.name for m in skill_library.gaps(env, task)}
    assert "frontend" not in missing


def test_install_writes_baseline(tmp_path):
    env = _env(tmp_path)
    md = skill_library.install(env, "security")
    assert md is not None and md.exists()
    body = skills.read_skill(env, "security")
    assert "OWASP" in body or "injection" in body


def test_install_unknown_domain_returns_none(tmp_path):
    env = _env(tmp_path)
    assert skill_library.install(env, "astrology") is None


def test_install_does_not_clobber_substantive_skill(tmp_path):
    env = _env(tmp_path)
    skill_dir = env / "skills" / "security"
    skill_dir.mkdir(parents=True)
    custom = ("---\nname: security\ndescription: ours\n---\n\n# security\n\n"
              + "Our project-specific rule.\n" * 30)
    (skill_dir / "SKILL.md").write_text(custom)
    skill_library.install(env, "security")
    body = skills.read_skill(env, "security")
    assert "project-specific rule" in body  # untouched


def test_gap_note_mentions_ensure_skill(tmp_path):
    env = _env(tmp_path)
    task = make_task(env, "T-3", title="Add login endpoint with JWT auth")
    note = skill_library.gap_note(env, task)
    assert "ensure_skill" in note
    assert "security" in note


def test_no_gap_note_when_covered(tmp_path):
    env = _env(tmp_path)
    skill_library.install(env, "security")
    task = make_task(env, "T-4", title="Add login endpoint with JWT auth",
                     description="just auth")
    # security covered; ensure no security gap remains
    note = skill_library.gap_note(env, task)
    assert "security" not in note


def test_reconcile_brief_lists_skills_and_instructions(tmp_path):
    env = _env(tmp_path)
    skill_library.install(env, "frontend")
    task = make_task(env, "T-9", title="Add stats screen",
                     description="charts and progress")
    brief = skill_library.reconcile_brief(env, tmp_path, task)
    assert "Reconcile skills" in brief
    assert "save_to_skill" in brief
    assert "ask_user" in brief
    assert "frontend" in brief          # existing skills listed
    assert "T-9" in brief               # task id shown


def test_reconcile_brief_handles_no_git(tmp_path):
    env = _env(tmp_path)
    task = make_task(env, "T-10", title="x")
    # tmp_path is not a git repo → no diff, must not raise
    brief = skill_library.reconcile_brief(env, tmp_path, task)
    assert "no git diff detected" in brief
