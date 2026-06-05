"""PRD (Product Requirements Document) parser.

PRDs live in ``harn_env/prd/`` as Markdown files with a YAML-style frontmatter
block.  They are written by product/analysts; the agent normalises them at loop
start so parsing is simple and predictable.

Required structure
------------------
::

    ---
    id: auth
    status: active          # draft | active | done
    priority: 10
    ---

    # PRD: Auth System

    ## Problem
    ...

    ## Goal
    ...

    ## Scope
    ...

    ## Acceptance criteria
    - ...

    ## Open questions
    -

If a PRD file is missing frontmatter or required sections the loop warns the
agent so it can normalise the file before proceeding.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^(\w+):\s*(.*)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

REQUIRED_SECTIONS = {"Problem", "Goal", "Scope", "Acceptance criteria"}

# Heading synonyms → canonical section name. Lets product/analysts write PRDs in
# their own language (e.g. Russian) while harn still checks the same structure.
_SECTION_ALIASES = {
    # Problem
    "problem": "Problem", "проблема": "Problem", "контекст": "Problem",
    # Goal
    "goal": "Goal", "goals": "Goal", "цель": "Goal", "цели": "Goal",
    "goal / success criteria": "Goal",
    # Scope
    "scope": "Scope", "содержание": "Scope", "объём": "Scope", "объем": "Scope",
    "рамки": "Scope",
    # Acceptance criteria
    "acceptance criteria": "Acceptance criteria",
    "критерии приёмки": "Acceptance criteria",
    "критерии приемки": "Acceptance criteria",
    "критерии готовности": "Acceptance criteria",
}


def _canonical_section(name: str) -> str:
    """Map a heading (any supported language) to its canonical name, else itself."""
    return _SECTION_ALIASES.get(name.strip().lower(), name.strip())


@dataclass
class Prd:
    id:       str
    path:     Path
    title:    str    = ""
    status:   str    = "draft"
    priority: int    = 50
    sections: dict[str, str] = field(default_factory=dict)
    raw:      str    = ""

    @property
    def missing_sections(self) -> list[str]:
        return [s for s in REQUIRED_SECTIONS if s not in self.sections]

    @property
    def is_normalised(self) -> bool:
        return not self.missing_sections


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter_dict, body_text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for km in _KV_RE.finditer(m.group(1)):
        fm[km.group(1).strip()] = km.group(2).strip()
    body = text[m.end():]
    return fm, body


def _parse_sections(body: str) -> dict[str, str]:
    """Split body on `## Heading` → {heading: content}."""
    sections: dict[str, str] = {}
    headings = list(_SECTION_RE.finditer(body))
    for i, h in enumerate(headings):
        name = _canonical_section(h.group(1))
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def _title_from_body(body: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else ""


def load(path: Path) -> Prd:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = _parse_frontmatter(text)
    return Prd(
        id=fm.get("id") or path.stem,
        path=path,
        title=_title_from_body(body),
        status=fm.get("status", "draft"),
        priority=int(fm.get("priority") or 50),
        sections=_parse_sections(body),
        raw=text,
    )


def load_all(env_dir: Path) -> list[Prd]:
    prd_dir = env_dir / "prd"
    if not prd_dir.exists():
        return []
    return [load(p) for p in sorted(prd_dir.glob("*.md"))]


def find(env_dir: Path, prd_id: str) -> Prd | None:
    key = prd_id.strip().lower()
    for p in load_all(env_dir):
        if p.id.lower() == key:
            return p
    return None


def normalisation_hint(prds: list[Prd]) -> str:
    """Return a prompt hint listing PRDs that need normalisation."""
    bad = [p for p in prds if not p.is_normalised]
    if not bad:
        return ""
    lines = ["⚠️  These PRD files are missing required sections and should be "
             "normalised before the task is worked:"]
    for p in bad:
        lines.append(f"  - {p.path.name}: missing {', '.join(p.missing_sections)}")
    lines.append("Add the missing sections to the PRD file, then continue.")
    return "\n".join(lines)
