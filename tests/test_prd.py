"""PRD parsing — frontmatter, sections, multilingual headings."""
from __future__ import annotations

from pathlib import Path
from harn import prd


def _prd(tmp_path: Path, body: str) -> prd.Prd:
    d = tmp_path / "prd"; d.mkdir(parents=True, exist_ok=True)
    p = d / "x.md"; p.write_text(body, encoding="utf-8")
    return prd.load(p)


def test_english_sections_normalised(tmp_path):
    p = _prd(tmp_path,
        "---\nid: x\nstatus: active\n---\n# PRD\n\n## Problem\na\n\n"
        "## Goal\nb\n\n## Scope\nc\n\n## Acceptance criteria\n- d\n")
    assert p.is_normalised
    assert p.status == "active"


def test_russian_sections_normalised(tmp_path):
    p = _prd(tmp_path,
        "---\nid: x\n---\n# PRD: Аутентификация\n\n## Проблема\nа\n\n"
        "## Цель\nб\n\n## Содержание\nв\n\n## Критерии приёмки\n- г\n")
    assert p.is_normalised                       # RU headings canonicalised
    assert set(p.sections) >= prd.REQUIRED_SECTIONS


def test_missing_section_flagged(tmp_path):
    p = _prd(tmp_path, "---\nid: x\n---\n# PRD\n\n## Problem\na\n")
    assert not p.is_normalised
    assert "Goal" in p.missing_sections
