"""Custom tools: user-added, agent-callable capabilities.

Each custom tool is one small JSON file under harn_env/tools/<name>.json —
mirrors harn/skills.py's "directory of files IS the source of truth" shape,
but structured (not prose) since a tool needs a real parameter list. A tool's
`command` is a shell template with `{param}` placeholders, executed the exact
same shlex-quoted, non-shell=True way harn/feedback.py's run_feedback() runs
a project's test command.
"""
from __future__ import annotations

import io
import json
import re
import shlex
import subprocess
import zipfile
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


def export_bundle(tool: CustomTool) -> bytes:
    """Export `tool` as a single portable file. A tool with no sibling
    script (source == 'chat', or an 'upload' tool whose command doesn't
    reference any file actually present alongside it) exports as plain JSON
    bytes -- the exact file save() already wrote. An 'upload' tool with a
    sibling script exports as a zip (stdlib zipfile) containing both the
    JSON and the script, so a single download always reconstructs the tool
    exactly on the importing side."""
    json_bytes = tool.path.read_bytes()
    # A script upload always names its file explicitly in `command` (e.g.
    # "bash lint.sh"), so bundle any OTHER file in the tool's directory that
    # `command` references by name -- this avoids bundling unrelated files
    # that happen to live in the same tools/ directory.
    referenced = [
        p for p in sorted(tool.path.parent.iterdir())
        if p.is_file() and p != tool.path and p.name in tool.command
    ]
    if not referenced:
        return json_bytes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{tool.name}.json", json_bytes)
        for p in referenced:
            zf.writestr(p.name, p.read_bytes())
    return buf.getvalue()


def import_bundle(env_dir: Path, data: bytes, filename: str) -> CustomTool:
    """Inverse of export_bundle(). Detects json-vs-zip by content (NOT by
    `filename`, which is attacker-controlled and untrusted -- it's used only
    for a friendlier error message, never to decide parsing behavior).

    SECURITY: `data` originates from another harn user's export and is
    therefore untrusted input, exactly like an uploaded tool script. This
    function persists the tool ONLY via the existing tools_mod.save(), which
    independently re-validates the tool name and every param name
    ([a-z0-9_]+) and rejects a name collision -- it does NOT write the tool's
    JSON to disk directly. save() raises ValueError on any of those
    rejections, which this function lets propagate to the caller unchanged.

    A zip bundle's script entry name is sanitized to its basename before
    being written to disk (Path(name).name, rejecting anything that reduces
    to '' , '.', or '..') -- the same path-traversal guard already applied
    to direct script uploads in studio.save_custom_tool_payload(), so a
    crafted zip entry name like '../../evil.sh' cannot escape the tools
    directory."""
    if zipfile.is_zipfile(io.BytesIO(data)):
        zf = zipfile.ZipFile(io.BytesIO(data))
        try:
            json_name = next(n for n in zf.namelist() if n.endswith(".json"))
        except StopIteration:
            raise ValueError(f"{filename}: zip bundle has no .json tool definition")
        parsed = json.loads(zf.read(json_name))
        p = save(
            env_dir, parsed.get("name", ""), parsed.get("description", ""),
            parsed.get("params", []), parsed.get("command", ""),
            source=parsed.get("source", "upload"),
        )
        for n in zf.namelist():
            if n == json_name:
                continue
            safe_name = Path(n).name  # strip any path components (traversal guard)
            if not safe_name or safe_name in (".", ".."):
                continue
            (p.parent / safe_name).write_bytes(zf.read(n))
        return read(env_dir, parsed["name"])
    parsed = json.loads(data)
    save(
        env_dir, parsed.get("name", ""), parsed.get("description", ""),
        parsed.get("params", []), parsed.get("command", ""),
        source=parsed.get("source", "chat"),
    )
    return read(env_dir, parsed["name"])
