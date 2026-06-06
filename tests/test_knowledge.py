"""Knowledge capture: answers/conventions saved into skills (Phase 2)."""
from __future__ import annotations

from pathlib import Path

from harn import skills


def test_append_learning_creates_skill(tmp_path):
    env = tmp_path
    md = skills.append_learning(env, "security", "All endpoints require auth.")
    assert md.exists()
    body = md.read_text()
    assert "name: security" in body
    assert "All endpoints require auth." in body
    assert skills._LEARNED_HEADING in body


def test_append_learning_appends_to_existing(tmp_path):
    env = tmp_path
    skills.append_learning(env, "security", "Rule one.")
    skills.append_learning(env, "security", "Rule two.")
    body = (env / "skills" / "security" / "SKILL.md").read_text()
    assert "Rule one." in body and "Rule two." in body
    assert body.count(skills._LEARNED_HEADING) == 1   # single growing section


def test_append_learning_normalises_name(tmp_path):
    skills.append_learning(tmp_path, "Front End", "Use TanStack Query.")
    assert (tmp_path / "skills" / "front-end" / "SKILL.md").exists()


def test_learned_fact_shows_in_index_and_body(tmp_path):
    env = tmp_path
    skills.append_learning(env, "testing", "Coverage must stay above 80%.")
    assert "testing" in skills.index(env)
    assert "Coverage must stay above 80%." in skills.read_skill(env, "testing")


def test_append_learning_preserves_existing_body(tmp_path):
    env = tmp_path
    d = env / "skills" / "standards"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: standards\ndescription: house style.\n---\n\n# standards\n"
        "Existing guidance here.\n"
    )
    skills.append_learning(env, "standards", "New learned rule.")
    body = (d / "SKILL.md").read_text()
    assert "Existing guidance here." in body
    assert "New learned rule." in body
