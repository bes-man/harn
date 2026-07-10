"""Custom tools: user-added, agent-callable capabilities.

Each custom tool is one small JSON file under harn_env/tools/<name>.json —
mirrors harn/skills.py's "directory of files IS the source of truth" shape,
but structured (not prose) since a tool needs a real parameter list. A tool's
`command` is a shell template with `{param}` placeholders, executed the exact
same shlex-quoted, non-shell=True way harn/feedback.py's run_feedback() runs
a project's test command.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def is_safe_param_name(name: str) -> bool:
    """True iff `name` is safe to splice into generated Python source as a
    function parameter (e.g. Task 2's `def _custom_tool({param}: str = ''):`
    exec-based registration). Same character class as tool names themselves
    — restrictive on purpose, since this is the last line of defense against
    code injection via an attacker-controlled param name (uploaded tool
    definition, agent-drafted tool, or an imported tool bundle from another
    harn user)."""
    return bool(_NAME_RE.fullmatch(name))


@dataclass
class CustomTool:
    name: str
    description: str
    params: list[str]
    command: str
    source: str
    path: Path


def _tools_dir(env_dir: Path) -> Path:
    return env_dir / "tools"


def discover(env_dir: Path) -> list[CustomTool]:
    d = _tools_dir(env_dir)
    if not d.exists():
        return []
    out: list[CustomTool] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(CustomTool(
            name=data.get("name", p.stem),
            description=data.get("description", ""),
            params=data.get("params", []),
            command=data.get("command", ""),
            source=data.get("source", "chat"),
            path=p,
        ))
    return out


def read(env_dir: Path, name: str) -> CustomTool | None:
    for t in discover(env_dir):
        if t.name == name:
            return t
    return None


def name_taken(env_dir: Path, name: str) -> bool:
    return read(env_dir, name) is not None


def save(env_dir: Path, name: str, description: str, params: list[str],
         command: str, source: str = "chat") -> Path:
    """Create a new custom tool. Raises ValueError on an invalid name or a
    name collision with an EXISTING CUSTOM tool — this function does NOT
    check the built-in MCP tool names (that would require importing
    mcp_server.py, which imports this module — the caller combines both
    checks via name_taken() + its own built-in-name set)."""
    name = name.strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid tool name: {name!r} (must match [a-z0-9_]+)")
    for param in params:
        if not is_safe_param_name(param):
            raise ValueError(
                f"invalid param name: {param!r} (must match [a-z0-9_]+)"
            )
    if name_taken(env_dir, name):
        raise ValueError(f"a custom tool named '{name}' already exists")
    d = _tools_dir(env_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps({
        "name": name,
        "description": description.strip(),
        "params": list(params),
        "command": command,
        "source": source,
    }, indent=2), encoding="utf-8")
    return p


def delete(env_dir: Path, name: str) -> bool:
    t = read(env_dir, name)
    if t is None:
        return False
    t.path.unlink()
    return True


def index(env_dir: Path) -> str:
    lines = [f"- {t.name}: {t.description}" for t in discover(env_dir)]
    return "\n".join(lines) if lines else "(no custom tools installed)"


def execute(tool: CustomTool, args: dict[str, str], cwd: Path,
           timeout: int = 600) -> str:
    cmd = tool.command
    for k, v in args.items():
        cmd = cmd.replace("{" + k + "}", shlex.quote(str(v)))
    try:
        proc = subprocess.run(
            shlex.split(cmd), cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout,
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return f"command not found: {exc}"
