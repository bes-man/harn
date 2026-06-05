"""`harn setup`: scaffold a harn_env into a target project.

Resolution layers (later overrides earlier):
  1. bundled templates in harn/templates  (the "base project"; edit in your fork)
  2. anything already present in the target project's harn_env (kept, not clobbered)

It also writes the cross-agent glue:
  - AGENTS.md at project root (portable instructions: Codex/Cursor/Antigravity/Claude)
  - per-agent MCP config so each agent can reach the harn MCP server
"""
from __future__ import annotations

import importlib.resources as resources
import json
import shutil
import sys
from pathlib import Path

from . import ENV_DIRNAME


def _templates_dir() -> Path:
    return Path(str(resources.files("harn") / "templates"))


def _copy_tree(src: Path, dst: Path) -> list[str]:
    created: list[str] = []
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            if target.exists():
                continue  # never clobber project-local edits
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            created.append(str(rel))
    return created


def _mcp_command() -> dict:
    # How an agent should launch the harn MCP server (stdio).
    return {
        "command": sys.executable,
        "args": ["-m", "harn", "mcp"],
        "env": {"HARN_ENV_DIR": "harn_env"},
    }


def _write_agent_configs(project_root: Path) -> list[str]:
    written = []
    from .config import Config, DEFAULTS
    from . import semble_bridge

    cmd = _mcp_command()
    # Load config from env_dir if already set up, otherwise use defaults
    env_dir = project_root / "harn_env"
    try:
        cfg = Config.load(env_dir)
    except Exception:
        from dataclasses import fields as _fields
        cfg = Config()  # defaults

    servers: dict = {"harn": cmd}
    servers.update(semble_bridge.mcp_servers(cfg))

    mcp_payload = json.dumps({"mcpServers": servers}, indent=2)

    # Claude Code: .mcp.json
    claude_cfg = project_root / ".mcp.json"
    if not claude_cfg.exists():
        claude_cfg.write_text(mcp_payload)
        written.append(".mcp.json (Claude)")

    # Cursor: .cursor/mcp.json
    cursor_cfg = project_root / ".cursor" / "mcp.json"
    if not cursor_cfg.exists():
        cursor_cfg.parent.mkdir(parents=True, exist_ok=True)
        cursor_cfg.write_text(mcp_payload)
        written.append(".cursor/mcp.json (Cursor)")

    # Codex (config.toml) and Antigravity (mcp_config.json) live in the user's
    # home dir with different formats; we drop a ready-to-paste snippet instead
    # of editing global files behind the user's back.
    snippet = project_root / ENV_DIRNAME / "mcp_snippets.md"
    snippet.write_text(
        "# Paste into each agent's MCP config\n\n"
        "## Codex  (~/.codex/config.toml)\n```toml\n"
        "[mcp_servers.harn]\n"
        f'command = "{cmd["command"]}"\n'
        f'args = {json.dumps(cmd["args"])}\n'
        "```\n\n"
        "## Antigravity  (~/.gemini/config/mcp_config.json)\n```json\n"
        + json.dumps({"mcpServers": {"harn": cmd}}, indent=2)
        + "\n```\n\n"
        "## Qwen Code  (~/.qwen/settings.json)\n```json\n"
        + json.dumps({"mcpServers": {"harn": cmd}}, indent=2)
        + "\n```\n"
    )
    written.append("harn_env/mcp_snippets.md (Codex + Antigravity + Qwen)")
    return written


def setup(project_root: Path) -> dict:
    project_root = project_root.resolve()
    env_dir = project_root / ENV_DIRNAME
    env_dir.mkdir(parents=True, exist_ok=True)

    created = _copy_tree(_templates_dir(), env_dir)

    # Move the bundled AGENTS.md to the project root (portable instruction file).
    bundled_agents = env_dir / "AGENTS.md"
    root_agents = project_root / "AGENTS.md"
    if bundled_agents.exists() and not root_agents.exists():
        shutil.move(str(bundled_agents), str(root_agents))
    elif bundled_agents.exists():
        bundled_agents.unlink()

    (env_dir / "state").mkdir(exist_ok=True)
    agent_cfgs = _write_agent_configs(project_root)

    return {"env_dir": str(env_dir), "created": created, "agent_configs": agent_cfgs}
