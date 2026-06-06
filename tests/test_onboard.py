"""Onboarding: stack detection + seeding skills with obvious facts."""
from __future__ import annotations

import json
from pathlib import Path

from harn import onboard, skills


def test_detect_python_fastapi(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['fastapi', 'pytest']\n[tool.ruff]\n")
    stack = onboard.detect_stack(tmp_path)
    assert "Python" in stack["languages"]
    assert "FastAPI" in stack["frameworks"]
    assert "pytest" in stack["tests"]
    assert "ruff" in stack["linters"]


def test_detect_node_react(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"react": "^18", "next": "^14"},
        "devDependencies": {"vitest": "^1", "eslint": "^9"},
    }))
    stack = onboard.detect_stack(tmp_path)
    assert "JavaScript/TypeScript" in stack["languages"]
    assert "React" in stack["frameworks"] and "Next.js" in stack["frameworks"]
    assert "vitest" in stack["tests"]
    assert "eslint" in stack["linters"]


def test_detect_empty_project(tmp_path):
    stack = onboard.detect_stack(tmp_path)
    assert stack["languages"] == []
    assert "no recognizable stack" in onboard.stack_summary(stack)


def test_seed_skills_writes_facts(tmp_path):
    env = tmp_path / "harn_env"
    env.mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies=['django','pytest']\n[tool.black]\n")
    stack = onboard.detect_stack(tmp_path)
    notes = onboard.seed_skills(env, stack)

    assert notes  # something was seeded
    project = skills.read_skill(env, "project")
    standards = skills.read_skill(env, "standards")
    assert "Django" in project
    assert "pytest" in standards and "black" in standards


def test_onboard_brief_mentions_docs(tmp_path):
    (tmp_path / "README.md").write_text("# My project")
    stack = onboard.detect_stack(tmp_path)
    brief = onboard.onboard_brief(stack)
    assert "ONBOARDING" in brief
    assert "README.md" in brief
    assert 'skill="' in brief  # tells agent to tag standard questions
