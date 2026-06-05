"""Code-search integration: semble + SocratiCode (Variant C).

Both backends are optional and config-controlled (`[code_search]` in harn.toml).
Both default to *enabled* — they gracefully degrade when not installed/running.

┌─────────────────────────────────────────────────────────────────────┐
│ Backend      │ Purpose              │ Install                       │
├─────────────────────────────────────────────────────────────────────┤
│ semble       │ Semantic chunk search│ included with harn (Python)   │
│ SocratiCode  │ Dependency graph /   │ npx -y socraticode@^1.8       │
│              │ blast-radius (static)│ (requires Docker + Qdrant)    │
└─────────────────────────────────────────────────────────────────────┘

Priority in oracle prompts: SocratiCode (static, precise) > semble (semantic).
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# semble
# ---------------------------------------------------------------------------

#: PyPI package; the MCP server lives in the `[mcp]` extra (verified on 0.3.2).
SEMBLE_PYPI = "semble[mcp]"
SEMBLE_VERSION_CONSTRAINT = ">=0.3.0,<1.0"
#: semble's actual MCP tool names — NO `semble_` prefix (verified via tools/list).
SEMBLE_TOOLS = ("search", "find_related")


def semble_installed() -> bool:
    """True when the semble Python package is importable."""
    return importlib.util.find_spec("semble") is not None


def semble_version() -> str:
    try:
        from importlib.metadata import version
        return version("semble")
    except Exception:
        return ""


def semble_server_cmd() -> list[str] | None:
    """Command that launches the semble MCP server, or None.

    The bare `semble` executable (installed via the `semble[mcp]` extra) IS the
    stdio MCP server — there is NO `semble mcp` subcommand. Falls back to `uvx`
    when the binary isn't on PATH but uv is available.
    """
    bin_ = shutil.which("semble")
    if bin_:
        return [bin_]
    if shutil.which("uvx"):
        return ["uvx", "--from", "semble[mcp]", "semble"]
    return None


# ---------------------------------------------------------------------------
# SocratiCode
# ---------------------------------------------------------------------------

#: npm package — pin to the ^1.8 series (tested with harn).
SOCRATICODE_NPM = "socraticode"
SOCRATICODE_VERSION_CONSTRAINT = "^1.8"   # semver: >=1.8.0 <2.0.0

# MCP tools exposed by SocratiCode that harn actively uses.
SOCRATICODE_TOOLS = (
    "codebase_impact",    # blast-radius: what breaks if symbol X changes
    "codebase_symbol",    # 360°: definition + callers + callees
    "codebase_flow",      # forward execution trace from entry point
    "codebase_search",    # hybrid semantic+BM25 search
)


def socraticcode_npx_available() -> bool:
    """True when npx is on PATH (SocratiCode can be launched)."""
    return shutil.which("npx") is not None


def socraticcode_server_entry() -> dict | None:
    """MCP server config entry for SocratiCode, or None when npx is absent."""
    if not socraticcode_npx_available():
        return None
    return {
        "command": "npx",
        "args": [
            "-y",
            f"{SOCRATICODE_NPM}@{SOCRATICODE_VERSION_CONSTRAINT}",
        ],
        "env": {
            # Qdrant runs locally via Docker by default; override if using cloud.
            # QDRANT_URL: http://localhost:6333
            # EMBEDDING_PROVIDER: ollama | openai | google
        },
    }


def _docker_running() -> bool:
    """True if a Docker daemon is reachable (SocratiCode needs Qdrant)."""
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True,
                           text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Installation — driven by `harn setup` for enabled backends
# ---------------------------------------------------------------------------

def install_semble(timeout: int = 900) -> tuple[bool, str]:
    """Ensure `semble[mcp]` is installed in the current environment."""
    if semble_installed():
        return True, f"semble already installed ({semble_version() or '?'})"
    spec = f"{SEMBLE_PYPI}{SEMBLE_VERSION_CONSTRAINT}"
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", spec],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"pip install {spec} failed to start: {e}"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        return False, f"pip install {spec} failed: {tail[0][:200]}"
    return True, f"installed {spec}"


def prepare_socraticode(prefetch: bool = True, timeout: int = 300) -> tuple[bool, str]:
    """Check prerequisites for SocratiCode and warm the npx cache.

    SocratiCode itself is fetched by npx on first use; harn can't install Docker
    or start Qdrant for the user, so this verifies and warns instead.
    """
    if not socraticcode_npx_available():
        return False, "npx not found — install Node.js to use SocratiCode"
    notes: list[str] = []
    if prefetch:
        # Download the package into the npx cache without starting the server.
        try:
            # `npm view <pkg> version` returns the single latest version.
            r = subprocess.run(
                ["npm", "view", SOCRATICODE_NPM, "version"],
                capture_output=True, text=True, timeout=timeout,
            )
            ver = (r.stdout or "").strip().splitlines()[-1:] or [""]
            if r.returncode == 0 and ver[0]:
                notes.append(f"SocratiCode reachable via npx (latest {ver[0]})")
            else:
                return False, "could not reach the socraticode npm package"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"npm view failed: {e}"
    if _docker_running():
        notes.append("Docker running — SocratiCode auto-starts Qdrant+Ollama "
                     "on first use (~5 min image pull, one time)")
    else:
        notes.append("⚠️  Docker not running — start Docker Desktop; SocratiCode "
                     "auto-pulls & runs Qdrant+Ollama itself (or set QDRANT_URL/"
                     "EMBEDDING_PROVIDER for cloud, no Docker needed)")
    return True, "; ".join(notes) or "SocratiCode prerequisites OK"


def ensure_backends(cfg, *, prefetch: bool = True) -> list[str]:
    """Install/verify the code-search backends enabled in config.

    Returns human-readable status lines for `harn setup` to print.
    """
    lines: list[str] = []
    if getattr(cfg, "code_search_semble", False):
        ok, msg = install_semble()
        lines.append(("✓ " if ok else "✗ ") + msg)
    if getattr(cfg, "code_search_socraticcode", False):
        ok, msg = prepare_socraticode(prefetch=prefetch)
        lines.append(("✓ " if ok else "✗ ") + msg)
    return lines


# ---------------------------------------------------------------------------
# Git diff parsing — extract (file, first_changed_line) for blast-radius calls
# ---------------------------------------------------------------------------

_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.MULTILINE)

# Patterns that identify non-production code — excluded from blast-radius analysis
_SKIP_PATTERNS = (
    "/test", "/tests/", "_test.", ".test.", "/spec/", ".spec.",
    "fixture", "/mock", "__pycache__", ".min.js", ".d.ts",
    "/vendor/", "/node_modules/",
)


def changed_files(diff: str) -> list[tuple[str, int]]:
    """Parse a git diff → [(file_path, first_changed_line), ...] (production only)."""
    if not diff:
        return []
    result: list[tuple[str, int]] = []
    sections = _DIFF_FILE_RE.split(diff)
    it = iter(sections)
    next(it, None)
    for path in it:
        hunks = next(it, "")
        path = path.strip()
        if not path or path == "/dev/null":
            continue
        if any(s in path.lower() for s in _SKIP_PATTERNS):
            continue
        m = _HUNK_RE.search(hunks)
        result.append((path, int(m.group(1)) if m else 1))
    return result


# ---------------------------------------------------------------------------
# Prompt helpers — injected into planning / oracle turns
# ---------------------------------------------------------------------------

def _semble_search_note() -> str:
    return (
        "> **semble** is available — use its MCP tools (your client may prefix "
        "them, e.g. `mcp__semble__search`):\n"
        "> - `search(\"<topic>\")` — retrieve only the relevant code chunks "
        "instead of reading whole files\n"
        "> - `find_related(\"<file>\", <line>)` — semantic neighbours of a line"
    )


def _socraticcode_note() -> str:
    return (
        "> **SocratiCode** is available — use these tools for static dependency "
        "analysis (more precise than semantic search for impact assessment):\n"
        "> - `codebase_impact(\"<symbol>\")` — what breaks if this symbol changes\n"
        "> - `codebase_symbol(\"<symbol>\")` — definition + callers + callees\n"
        "> - `codebase_search(\"<query>\")` — hybrid semantic+BM25 search"
    )


def planning_hint(cfg) -> str:
    """Return code-search guidance for the planning-turn prompt."""
    parts: list[str] = []
    if cfg.code_search_socraticcode and socraticcode_npx_available():
        parts.append(_socraticcode_note())
        parts.append(
            "Before writing acceptance criteria, call `codebase_search(\"<task "
            "topic>\")` to understand existing patterns in the codebase."
        )
    elif cfg.code_search_semble and semble_installed():
        parts.append(_semble_search_note())
        parts.append(
            "Before writing acceptance criteria, call semble's `search(\"<task "
            "topic>\")` to understand existing patterns in the codebase."
        )
    return "\n".join(parts)


def oracle_hint(cfg, diff: str) -> str:
    """Return blast-radius guidance for the oracle-turn prompt."""
    changed = changed_files(diff)
    parts: list[str] = []

    if cfg.code_search_socraticcode and socraticcode_npx_available():
        # Static dependency graph is the most precise oracle tool
        parts.append(_socraticcode_note())
        if changed:
            parts.append(
                "**Blast-radius analysis (static, diff-scoped):**\n"
                "For each symbol changed in the diff, call `codebase_impact` to "
                "find everything that depends on it. Start with these files:\n"
                + "\n".join(f"  - `{p}` (line {l})" for p, l in changed)
            )
        else:
            parts.append(
                "Use `codebase_impact(\"<changed_symbol>\")` to verify blast radius."
            )

    elif cfg.code_search_semble and semble_installed():
        # Semantic fallback
        parts.append(_semble_search_note())
        if changed:
            calls = "\n".join(
                f"  - `find_related(\"{p}\", {l})`"
                for p, l in changed
            )
            parts.append(
                "**Semantic blast-radius (diff-scoped) — call semble's "
                "`find_related`:**\n" + calls + "\n"
                "Examine the returned chunks for unintended dependencies."
            )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Scaffold helpers — called by harn/scaffold.py
# ---------------------------------------------------------------------------

def mcp_servers(cfg) -> dict:
    """Return MCP server entries for semble and/or SocratiCode per config."""
    servers: dict = {}
    if cfg.code_search_semble:
        cmd = semble_server_cmd()
        if cmd:
            # bare `semble` takes no --repo flag; the search path is a tool arg.
            servers["semble"] = {"command": cmd[0], "args": cmd[1:]}
    if cfg.code_search_socraticcode:
        entry = socraticcode_server_entry()
        if entry:
            servers["socraticode"] = entry
    return servers
