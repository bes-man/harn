"""`harn setup`: scaffold a harn_env into a target project.

Resolution layers (later overrides earlier):
  1. bundled templates in harn/templates  (the "base project"; edit in your fork)
  2. anything already present in the target project's harn_env (kept, not clobbered)

Clean-root rule: harn writes content only under `harn_env/`. The only files it
adds to the project root are the connectors an agent's own discovery REQUIRES
(`.mcp.json` for Claude Code, `.cursor/mcp.json` for Cursor, `AGENTS.md`), it
creates them only for the configured agent(s), and it auto-adds them to
`.gitignore`. `harn teardown` removes them.
"""
from __future__ import annotations

import importlib.resources as resources
import json
import shutil
import sys
from pathlib import Path

from . import ENV_DIRNAME

_GITIGNORE_HEADER = "# harn (generated — agent connectors; safe to remove)"
_CODEX_BEGIN = "# >>> harn generated mcp"
_CODEX_END = "# <<< harn generated mcp"


def _templates_dir() -> Path:
    return Path(str(resources.files("harn") / "templates"))


# Top-level template files handled explicitly (not bulk-copied into harn_env):
# the AGENTS.md variants are written to the project ROOT per the guidance mode.
_SKIP_TOP = {"AGENTS.md", "AGENTS.full.md"}


def _copy_tree(src: Path, dst: Path) -> list[str]:
    created: list[str] = []
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if rel.parts and rel.parts[0] in _SKIP_TOP and len(rel.parts) == 1:
            continue
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


def _guidance_mode(project_root: Path) -> str:
    from .config import Config
    try:
        return Config.load(project_root / ENV_DIRNAME).guidance
    except Exception:
        return "lean"


def _agents_template_text(mode: str) -> str:
    """Return the AGENTS.md body for the guidance mode ('full' → verbose
    variant, else the lean core)."""
    tpl = _templates_dir() / ("AGENTS.full.md" if mode == "full" else "AGENTS.md")
    if not tpl.exists():
        tpl = _templates_dir() / "AGENTS.md"
    return tpl.read_text(encoding="utf-8")


def _mcp_command() -> dict:
    # How an agent should launch the harn MCP server (stdio).
    return {
        "command": sys.executable,
        "args": ["-m", "harn", "mcp"],
        "env": {"HARN_ENV_DIR": "harn_env"},
    }


def _mcp_servers(project_root: Path) -> dict:
    from .config import Config
    from . import semble_bridge
    env_dir = project_root / ENV_DIRNAME
    try:
        cfg = Config.load(env_dir)
    except Exception:
        cfg = Config()
    servers: dict = {"harn": _mcp_command()}
    servers.update(semble_bridge.mcp_servers(cfg))
    # Browser verification drives the live app through the Playwright MCP
    # server. Enable via [browser] in harn.toml, then re-run `harn setup`.
    if cfg.browser_enabled:
        servers["playwright"] = {
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
        }
    # context7: up-to-date library documentation on demand — the agent checks
    # current APIs/best practices instead of relying on stale training data.
    # Disable with [mcp] context7 = false in harn.toml.
    if getattr(cfg, "mcp_context7", True):
        servers["context7"] = {
            "command": "npx",
            "args": ["-y", "@upstash/context7-mcp"],
        }
    return servers


def _agent_chain(project_root: Path) -> list[str]:
    from .config import Config
    try:
        return Config.load(project_root / ENV_DIRNAME).agent_chain
    except Exception:
        return ["claude"]


# Connectors harn may place in the project root, keyed by the agent that needs
# them. Codex/Antigravity/Qwen read MCP config from the user's home dir, so they
# get a paste-snippet in harn_env instead of a root file.
def _root_connectors_for(chain: list[str]) -> set[str]:
    paths: set[str] = set()
    if "claude" in chain:
        paths.add(".mcp.json")
    if "cursor" in chain:
        paths.add(".cursor/mcp.json")
    if "codex" in chain:
        paths.add(".codex/config.toml")
    return paths


def _codex_mcp_block(servers: dict) -> str:
    lines = [_CODEX_BEGIN]
    for name, server in servers.items():
        lines += [f"[mcp_servers.{name}]",
                  f"command = {json.dumps(server['command'])}",
                  f"args = {json.dumps(server.get('args') or [])}"]
        env = server.get("env") or {}
        if env:
            lines.append(f"[mcp_servers.{name}.env]")
            lines += [f"{key} = {json.dumps(str(value))}" for key, value in env.items()]
    lines.append(_CODEX_END)
    return "\n".join(lines) + "\n"


def write_codex_mcp_config(project_root: Path, servers: dict) -> Path:
    """Upsert only Harn's marked MCP block, preserving user Codex settings."""
    path = project_root / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if _CODEX_BEGIN in text and _CODEX_END in text:
        before, rest = text.split(_CODEX_BEGIN, 1)
        _, after = rest.split(_CODEX_END, 1)
        text = before.rstrip() + "\n\n" + _codex_mcp_block(servers) + after.lstrip("\n")
    else:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + _codex_mcp_block(servers)
    path.write_text(text, encoding="utf-8")
    return path


def _write_agent_configs(project_root: Path) -> tuple[list[str], list[str]]:
    """Write only the connectors the configured agent(s) need.

    Returns (human_messages, root_relative_paths_created)."""
    written: list[str] = []
    root_paths: list[str] = []
    chain = _agent_chain(project_root)
    payload = json.dumps({"mcpServers": _mcp_servers(project_root)}, indent=2)

    # Connectors are generated, git-ignored files — refresh them whenever the
    # derived content changes (e.g. [browser]/[mcp]/[code_search] toggled and
    # `harn setup` re-run), otherwise toggles silently do nothing.
    if "claude" in chain:
        p = project_root / ".mcp.json"
        if not p.exists() or p.read_text() != payload:
            p.write_text(payload)
            written.append(".mcp.json (Claude Code)")
        root_paths.append(".mcp.json")

    if "cursor" in chain:
        p = project_root / ".cursor" / "mcp.json"
        if not p.exists() or p.read_text() != payload:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(payload)
            written.append(".cursor/mcp.json (Cursor)")
        root_paths.append(".cursor/mcp.json")

    if "codex" in chain:
        path = write_codex_mcp_config(project_root, _mcp_servers(project_root))
        written.append(".codex/config.toml (Codex CLI)")
        root_paths.append(".codex/config.toml")

    # Home-dir agents → paste snippet inside harn_env (no root files).
    others = [a for a in chain if a in ("codex", "antigravity", "qwen")]
    if others:
        cmd = _mcp_command()
        snippet = project_root / ENV_DIRNAME / "mcp_snippets.md"
        snippet.write_text(
            "# Paste into each agent's MCP config (home dir, not the project)\n\n"
            "## Codex  (~/.codex/config.toml)\n```toml\n"
            "[mcp_servers.harn]\n"
            f'command = "{cmd["command"]}"\nargs = {json.dumps(cmd["args"])}\n```\n\n'
            "## Antigravity  (~/.gemini/config/mcp_config.json)\n```json\n"
            + json.dumps({"mcpServers": {"harn": cmd}}, indent=2) + "\n```\n\n"
            "## Qwen Code  (~/.qwen/settings.json)\n```json\n"
            + json.dumps({"mcpServers": {"harn": cmd}}, indent=2) + "\n```\n"
        )
        written.append("harn_env/mcp_snippets.md (Codex/Antigravity/Qwen)")
    return written, root_paths


def refresh_agent_connectors(project_root: Path) -> None:
    """Refresh generated project MCP connectors before every headless run."""
    _, root_paths = _write_agent_configs(project_root)
    _gitignore_add(project_root, root_paths)


def _gitignore_add(project_root: Path, rel_paths: list[str]) -> None:
    """Ensure harn's root connectors are git-ignored (clean project history)."""
    if not rel_paths:
        return
    gi = project_root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    lines = existing.splitlines()
    to_add = [p for p in (["AGENTS.md", *rel_paths]) if p not in lines]
    if not to_add:
        return
    block = ([""] if existing and not existing.endswith("\n\n") else []) + \
        [_GITIGNORE_HEADER, *to_add, ""]
    gi.write_text((existing.rstrip("\n") + "\n" if existing else "") +
                  "\n".join(block), encoding="utf-8")


def setup(project_root: Path) -> dict:
    project_root = project_root.resolve()
    env_dir = project_root / ENV_DIRNAME
    env_dir.mkdir(parents=True, exist_ok=True)

    created = _copy_tree(_templates_dir(), env_dir)

    # AGENTS.md sits in the project ROOT (agents read it there), written from
    # the lean core or the full variant per [harn] guidance (harn.toml was just
    # copied, so the config is now readable).
    root_agents = project_root / "AGENTS.md"
    if not root_agents.exists():
        root_agents.write_text(
            _agents_template_text(_guidance_mode(project_root)), encoding="utf-8")
        created.append("AGENTS.md")

    # Claude Code auto-loads CLAUDE.md, NOT AGENTS.md — without this, the harn
    # protocol never reaches a Claude Code session. Write a thin CLAUDE.md that
    # imports AGENTS.md and front-loads the single most-violated rule.
    _write_claude_md(project_root)

    # A new project starts with EMPTY tasks/ and prd/ — no demo content. The
    # agent fills them during onboarding; samples live in harn_example/.
    # `agents/` is where role definitions (<name>.md) live — also empty by
    # default (see the agent-roles spec).
    for sub in ("state", "tasks", "prd", "design", "services", "agents"):
        (env_dir / sub).mkdir(exist_ok=True)

    # Secrets a role declares by NAME (harn_env/agents/<name>.md `secrets:`)
    # live here as KEY=value lines, chmod 600, never committed — see
    # secrets_store.py and the agent-roles spec's "Secrets" section.
    from . import secrets_store
    secrets_store.ensure_file(env_dir)

    # The single-file workflow the agent follows (flow + likely skills). Written
    # after the skill templates are copied so the skills index is populated;
    # refreshed by `harn onboard` once more skills are seeded.
    from . import workflow
    workflow.write(env_dir)
    if workflow.FILENAME not in created:
        created.append(workflow.FILENAME)

    agent_cfgs, root_paths = _write_agent_configs(project_root)
    _gitignore_add(project_root, [*root_paths, "harn_env/secrets.env"])

    return {
        "env_dir": str(env_dir),
        "created": created,
        "agent_configs": agent_cfgs,
        "root_paths": ["AGENTS.md", "CLAUDE.md", *root_paths],
    }


_CLAUDE_MD = """\
# Claude Code — project instructions

This project is driven by **harn**. The full protocol is in AGENTS.md, imported
below. Follow it for all work.

## ⚠️ STEP 0 — load the harn tools (they may be deferred)
Claude Code defers MCP tool schemas when many servers are connected: harn's
tools (`ask_user`, `get_next_task`, `list_services`, …) then appear only as
names and FAIL if called directly. **Before your first action in EVERY
session**, load them in one call:

    ToolSearch(query: "harn", max_results: 30)

Same applies to semble / socraticode / context7 tools when you need them
(`ToolSearch(query: "context7")`, etc.). Never skip harn because its tools
weren't loaded — loading them IS your first step. Working around harn (editing
files directly without `get_next_task` → pre-task protocol → `run_tests` →
`submit_for_review`) is a protocol violation EVEN for one-line changes.

## ⚠️ STEP 0.5 — orient (first turn)
Read `harn_env/WORKFLOW.md` (flow + likely skills). If `harn_env/state/ONBOARD.md`
exists and the PRD/standards are empty, onboard first (per that file).

## ⚠️ STEP 1 — every code-change request goes through harn, with skills
A request typed in the chat ("fix the padding", "add a button") is NOT exempt
from the protocol — it's just a task that doesn't exist yet:

1. No matching task on the board? → `create_task` (one sentence is fine), then
   `get_next_task` to claim it. This is 2 tool calls — cheaper than one rework.
2. **Skills on EVERY request**: `list_skills` + `read_skill` for every skill
   relevant to the change, and NAME them ("Loaded: frontend, standards") — even
   for one-liners. An edit that ignores a project standard is a bug you just
   haven't found yet.
3. **Plan mode first** for anything non-trivial (more than one file, any
   ambiguity, or touching behavior): enter your client's plan mode (Claude
   Code: EnterPlanMode / plan mode; Cursor/Codex: Plan Mode) and walk the
   pre-task protocol there — AS IS → TO BE → skills → best practices
   (context7) → clarifying questions. Code starts only after the plan (and any
   questions) are resolved. Trivial one-liners may skip plan mode but NEVER
   skip skills + task.

## ⚠️ The one rule that's easy to miss
**Never put a question or choice to the human as trailing chat prose.** ANY time
you would end a turn asking the user to decide — "shall I proceed?", "build the
module next?", "approach A or B?", a clarifying question — present it through the
native **`AskUserQuestion`** tool (interactive clickable options). For ambiguous
requirements or durable standards, ALSO call harn's `ask_user(question, skill=…)`
so the answer is saved into a skill. A question typed as plain prose is a bug.

@AGENTS.md
"""


def _write_claude_md(project_root: Path) -> str | None:
    """Write/refresh root CLAUDE.md so Claude Code (which auto-loads CLAUDE.md,
    not AGENTS.md) gets the harn protocol via an @AGENTS.md import. Backs up any
    existing file to CLAUDE.md.bak. Returns the path, or None if unchanged."""
    dest = project_root / "CLAUDE.md"
    if dest.exists():
        if dest.read_text(encoding="utf-8") == _CLAUDE_MD:
            return None
        # Only back up / overwrite a harn-managed file (contains our @AGENTS.md
        # import). If the user has their own CLAUDE.md without it, append-import
        # instead of clobbering.
        cur = dest.read_text(encoding="utf-8")
        if "@AGENTS.md" not in cur:
            dest.write_text(cur.rstrip() + "\n\n@AGENTS.md\n", encoding="utf-8")
            return str(dest)
        shutil.copy2(dest, dest.with_suffix(".md.bak"))
    dest.write_text(_CLAUDE_MD, encoding="utf-8")
    return str(dest)


def refresh_agents_md(project_root: Path) -> str | None:
    """Re-copy the bundled AGENTS.md into the project root, replacing the old
    harness-managed copy so template improvements (new protocol rules) reach
    existing projects. Backs up the current file to AGENTS.md.bak. Returns the
    path written, or None if it was already identical / no template found.

    AGENTS.md is harness-managed (onboarding fills the PRD + skills, not this
    file), so refreshing it is safe; the backup covers any local edits. The
    variant (lean vs full) follows [harn] guidance. Guidance topic files in
    harn_env/guidance/ are refreshed too (new ones added; existing left)."""
    project_root = project_root.resolve()
    new_text = _agents_template_text(_guidance_mode(project_root))
    dest = project_root / "AGENTS.md"
    changed = None
    if not dest.exists() or dest.read_text(encoding="utf-8") != new_text:
        if dest.exists():
            shutil.copy2(dest, dest.with_suffix(".md.bak"))
        dest.write_text(new_text, encoding="utf-8")
        changed = str(dest)
    # Ensure the on-demand guidance topics exist (add any new ones).
    g_src = _templates_dir() / "guidance"
    if g_src.exists():
        g_dst = project_root / ENV_DIRNAME / "guidance"
        g_dst.mkdir(parents=True, exist_ok=True)
        for f in g_src.glob("*.md"):
            t = g_dst / f.name
            if not t.exists():
                shutil.copy2(f, t)
                changed = changed or str(t)
    # Always ensure CLAUDE.md exists and imports AGENTS.md (Claude Code path).
    claude = _write_claude_md(project_root)
    return changed or claude


def teardown(project_root: Path) -> list[str]:
    """Remove the root connectors harn created (leaves harn_env/ intact)."""
    project_root = project_root.resolve()
    removed: list[str] = []
    chain = _agent_chain(project_root)
    candidates = ["AGENTS.md", *sorted(_root_connectors_for(chain))]
    for rel in candidates:
        p = project_root / rel
        if p.exists():
            if rel == ".codex/config.toml":
                text = p.read_text(encoding="utf-8")
                if _CODEX_BEGIN in text and _CODEX_END in text:
                    before, rest = text.split(_CODEX_BEGIN, 1)
                    _, after = rest.split(_CODEX_END, 1)
                    p.write_text((before.rstrip()+"\n"+after.lstrip("\n")).strip()+"\n",
                                 encoding="utf-8")
                    removed.append(rel + " (harn block)")
            else:
                p.unlink()
                removed.append(rel)
    # tidy empty .cursor dir
    cur = project_root / ".cursor"
    if cur.is_dir() and not any(cur.iterdir()):
        cur.rmdir()
    return removed
