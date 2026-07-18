"""Agent roles: `harn_env/agents/<name>.md` is a named role (a virtual
employee) that services one board status — see
docs/superpowers/specs/2026-07-18-agent-roles-design.md.

Discovery mirrors `skills.py`/`tools.py`'s directory-of-files pattern: the
directory IS the source of truth, frontmatter is parsed best-effort, invalid
definitions are skipped rather than crashing the whole discovery pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_value(raw: str):
    v = raw.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [x.strip() for x in inner.split(",") if x.strip()] if inner else []
    return v


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text)
    fm: dict = {}
    if not m:
        return fm
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key:
            fm[key] = _parse_value(val)
    return fm


@dataclass
class Role:
    name: str
    path: Path
    command: str = ""              # telegram slash command (triggers spec)
    status: str = ""               # board status this role services
    trigger: str = "manual"        # auto | manual
    next_status: str = ""          # empty = no transition on success
    workflow: str = ""             # workflow preset its runs execute
    oracle: bool = True            # independent re-check gates the transition
    secrets: list = field(default_factory=list)  # required env var NAMES
    isolation: str = "main"        # main | worktree
    agent: str = ""                # CLI adapter override (empty = project default)
    model: str = ""                # model override (empty = project default)

    def body(self) -> str:
        """The role's persona/instructions — everything after the frontmatter,
        injected into every step prompt this role runs (see loop.py's
        `role_note` param on `_build_step_prompt`/`run_step`)."""
        text = self.path.read_text(encoding="utf-8", errors="replace")
        return _FM_RE.sub("", text, count=1).strip()

    def prompt_note(self) -> str:
        """`## Role` block for the step prompt: persona + which secret env
        var NAMES are available — never their values (see `secrets_store`)."""
        parts = [f"## Role: {self.name}", self.body()]
        if self.secrets:
            parts.append(
                "Available secret environment variables (names only — read "
                "them from the process environment, e.g. os.environ, never "
                "ask the human for their values): " + ", ".join(self.secrets)
            )
        return "\n\n".join(p for p in parts if p.strip())


def discover(env_dir: Path) -> list[Role]:
    roles_dir = env_dir / "agents"
    if not roles_dir.exists():
        return []
    out: list[Role] = []
    for role_md in sorted(roles_dir.glob("*.md")):
        try:
            text = role_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _frontmatter(text)
        name = str(fm.get("name") or role_md.stem).strip()
        status = str(fm.get("status") or "").strip()
        if not name or not status:
            continue
        oracle_raw = fm.get("oracle", True)
        out.append(Role(
            name=name,
            path=role_md,
            command=str(fm.get("command") or name).strip(),
            status=status,
            trigger=(str(fm.get("trigger") or "manual").strip().lower()
                    if str(fm.get("trigger") or "manual").strip().lower() in ("auto", "manual")
                    else "manual"),
            next_status=str(fm.get("next_status") or "").strip(),
            workflow=str(fm.get("workflow") or "").strip(),
            oracle=oracle_raw if isinstance(oracle_raw, bool) else True,
            secrets=list(fm.get("secrets") or []),
            isolation=(str(fm.get("isolation") or "main").strip().lower()
                      if str(fm.get("isolation") or "main").strip().lower() in ("main", "worktree")
                      else "main"),
            agent=str(fm.get("agent") or "").strip(),
            model=str(fm.get("model") or "").strip(),
        ))
    return out


def index(env_dir: Path) -> str:
    lines = [f"- {r.name} (/{r.command}): services status '{r.status}'"
            for r in discover(env_dir)]
    return "\n".join(lines) if lines else "(no agent roles defined)"


def find(env_dir: Path, name: str) -> Role | None:
    for r in discover(env_dir):
        if r.name == name or r.command == name:
            return r
    return None


def _safe_name(name: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Za-z0-9._-]", "-", name.strip()) or "agent"


_ROLE_FM_FIELDS = ("name", "command", "status", "trigger", "next_status",
                   "workflow", "oracle", "secrets", "isolation", "agent", "model")


def _render_role(data: dict) -> str:
    def fmt(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, list):
            return "[" + ", ".join(str(x) for x in v) + "]"
        return str(v)
    lines = ["---"]
    for k in _ROLE_FM_FIELDS:
        if k in data and data[k] not in (None, ""):
            lines.append(f"{k}: {fmt(data[k])}")
    lines.append("---")
    body = (data.get("body") or "").strip()
    return "\n".join(lines) + "\n\n" + body + "\n"


def save(env_dir: Path, data: dict) -> Path:
    name = _safe_name(str(data.get("name") or ""))
    d = env_dir / "agents"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    path.write_text(_render_role({**data, "name": name}), encoding="utf-8")
    return path


def delete(env_dir: Path, name: str) -> bool:
    path = env_dir / "agents" / f"{_safe_name(name)}.md"
    if path.exists():
        path.unlink()
        return True
    return False
