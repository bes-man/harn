"""Configuration loading for a materialized harn_env."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore


def autonomy_directive(level: float) -> str:
    """Translate the 0.0–1.0 autonomy level into a behavioural directive.
    Low = ask about almost everything; high = decide and proceed. Surfaced to
    BOTH the headless loop and the chat agent (via get_next_task) so the
    harn.toml setting actually governs how often the agent asks."""
    pct = int(round(level * 100))
    if level <= 0.3:
        stance = (
            "Be METICULOUS. Surface every ambiguity, missing detail, or "
            "assumption and call `ask_user` for genuine product decisions. Do "
            "not request permission for routine tool calls or reversible work; "
            "execute those directly. Prefer asking when the answer materially "
            "changes the outcome, because the human wants tight control over "
            "direction."
        )
    elif level <= 0.7:
        stance = (
            "Be BALANCED. Decide routine, low-risk, reversible matters yourself "
            "using best practices and state your assumption. Reserve `ask_user` "
            "for choices that are BOTH ambiguous AND significant or hard to undo."
        )
    else:
        stance = (
            "Be DECISIVE and creative. Resolve ambiguity yourself from the "
            "project context, connected tools, and loaded skills. Do not wait "
            "for permission to take routine, reversible actions. State important "
            "assumptions and record them via `record_decision`. Only `ask_user` "
            "when truly blocked or a decision is high-stakes AND irreversible."
        )
    return f"## Autonomy: {pct}% self-directed\n{stance}"


def _clamp01(value) -> float:
    """Parse a 0.0–1.0 autonomy value, clamped into range (default 0.7)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, v))


def _nonneg_float(v) -> float:
    """Parse a non-negative float; negative or malformed values clamp to 0."""
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.0


def _nonneg_int(v) -> int:
    """Parse a non-negative int; negative or malformed values clamp to 0."""
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


_HIL_CHANNELS = {"chat", "telegram", "both"}


def _hil_channel(value: str) -> str:
    ch = (os.environ.get("HARN_HIL_CHANNEL") or value or "both").strip().lower()
    return ch if ch in _HIL_CHANNELS else "both"


DEFAULTS: dict = {
    # `agents` (a list) is the multi-agent form; `agent` (a string) is the
    # single-agent shorthand kept for back-compat. Either way the agents share
    # all context (AGENTS.md, the task board, PROGRESS.md, ANSWERS.md, MCP), so
    # whichever one runs next understands what's done and what's planned.
    "harn": {"agent": "claude", "agents": [], "project": "prj001",
             "autonomy": 0.7, "require_mcp": True, "guidance": "lean",
             # Default model for every harn run stage that has no per-stage
             # override; empty = let the CLI use its own default.
             "model": ""},
    "feedback": {"test_cmd": "", "require_tests": True},
    "loop": {"max_iterations": 10, "loop_aware": True,
             "auto": False, "auto_max_iterations": 30,
             "oracle": True, "oracle_agent": "",
             "design": True, "auto_reconcile": True,
             "max_cost_usd": 3.0, "max_tokens": 400000,
             "turn_timeout_seconds": 1800},
    "browser": {"enabled": False, "app_cmd": "", "app_url": "",
                "ready_timeout_s": 60},
    "code_search": {"semble": True, "socraticcode": True},
    "mcp": {"context7": True, "ui_supervise": True, "ui_port": 8765,
            "tool_reload_seconds": 2},
    "notify": {"idle_minutes": 30, "wait_for_reply": True, "wait_timeout_minutes": 0,
               "channel": "both", "chat_grace_minutes": 2},
    # Change logging: keep a release-notes-style changelog on each task (what
    # shipped + decisions/standards), for assembling documentation later.
    "log": {"changes": True},
    # Custom board pipeline. Empty = the built-in 5-status lifecycle
    # (todo/in_progress/review/changes_requested/done), unchanged. See
    # `_parse_board_statuses` for the two accepted TOML shapes.
    "board": {},
    "git": {"pr_base": "", "branch_prefix": "harn/", "push_remote": "origin"},
    "intake": {"confirm_before_run": True},
}


def _parse_board_statuses(board: dict) -> list[dict]:
    """Normalize the `[board]` section into an ordered list of
    ``{"name": str, "external": str | None}`` dicts.

    Two accepted TOML shapes:
        [board]
        statuses = ["new", "analyzing", "done"]
    or
        [[board.status]]
        name = "analyzing"
        external = "In Analysis"

    Empty/missing config -> empty list (caller falls back to the built-in
    five-status lifecycle for full backward compatibility).
    """
    entries = board.get("status")
    if isinstance(entries, list) and entries:
        out = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            name = str(e.get("name") or "").strip()
            if not name:
                continue
            external = e.get("external")
            out.append({"name": name, "external": str(external).strip() if external else None})
        if out:
            return out
    names = board.get("statuses")
    if isinstance(names, list):
        return [{"name": str(n).strip(), "external": None} for n in names if str(n).strip()]
    return []


@dataclass
class Config:
    agent: str = "claude"
    agents: list[str] = field(default_factory=list)
    # Default model applied to every harn run stage lacking a per-stage
    # override (empty = each CLI's own default). From `[harn] model`.
    model: str = ""
    project: str = "prj001"   # project code for the prj…-prd…-task… scheme
    # How self-directed the agent is, 0.0–1.0. 0 = meticulous (clarify
    # everything), 1 = creative (decide for itself). Default 0.7.
    autonomy: float = 0.7
    require_mcp: bool = True   # setup/doctor insist the MCP server is enabled
    # Guidance verbosity: "lean" = compact core AGENTS.md + on-demand
    # harn_env/guidance/ (fewer tokens); "full" = everything inline (max
    # predictability, more tokens).
    guidance: str = "lean"
    test_cmd: str = ""
    # Test-writing gate: when a turn changes code but adds/changes no tests,
    # feed back one "add tests" nudge before the work can proceed to verify.
    require_tests: bool = True
    max_iterations: int = 10
    loop_aware: bool = True
    auto: bool = False
    auto_max_iterations: int = 30
    # Run-level spend ceilings (0 = unlimited). Checked between turns in
    # loop.run(): crossing either stops the run and BLOCKS, so a single
    # runaway turn can't quietly burn the budget.
    max_cost_usd: float = 3.0
    max_tokens: int = 400000
    # Per-turn subprocess timeout handed to adapter.run_turn (0 = the
    # adapter's own 1800s default). Caps a single turn's blast radius.
    turn_timeout_seconds: int = 1800
    # harn ui supervises an owned `harn mcp --http` child on this port.
    mcp_ui_supervise: bool = True
    mcp_ui_port: int = 8765
    # Hot-reload: the MCP watcher polls harn_env/tools/ this often (0 = off).
    mcp_tool_reload_seconds: int = 2
    # Code search backends (both default on; gracefully degrade if not installed)
    code_search_semble: bool = True       # use semble for semantic chunk retrieval
    code_search_socraticcode: bool = True  # use SocratiCode for dependency graphs
    # context7 MCP: up-to-date library docs for the agent (default on; set
    # [mcp] context7 = false to drop it from the generated MCP config).
    mcp_context7: bool = True
    # Oracle turn: after verify, an independent agent with fresh context checks
    # whether the work actually solves the problem and flags technical debt.
    oracle: bool = True
    oracle_agent: str = ""   # empty = same as main agent
    # Knowledge capture: when a task hits review, `harn watch` runs a reconcile
    # turn headless (enrich skills/services + changelog) so capture doesn't depend
    # on the chat agent remembering to. Default on.
    auto_reconcile: bool = True
    # Design turn: for user-facing tasks, planning generates an HTML mockup
    # (harn_env/design/<task_id>.html) and confirms it with the human BEFORE
    # implementation; executor/oracle then build/verify against it.
    design: bool = True
    # Browser verification: after tests+verify pass, drive the real app through
    # the Playwright MCP server and check the acceptance criteria in the UI.
    browser_enabled: bool = False
    app_cmd: str = ""              # how to start the app; empty = already running
    app_url: str = ""              # where the app answers, e.g. http://localhost:3000
    ready_timeout_s: int = 60      # how long to wait for app_url after app_cmd
    idle_minutes: int = 30
    wait_for_reply: bool = True
    wait_timeout_minutes: int = 0
    # HIL routing: where a blocking question goes and how long to wait in the
    # chat before escalating to Telegram.
    hil_channel: str = "both"          # chat | telegram | both
    chat_grace_minutes: int = 2        # 0 = escalate to Telegram immediately
    # Keep a release-notes-style changelog per task (record_change + reconcile
    # backstop), assembled into docs via `harn changelog` / generate_changelog.
    log_changes: bool = True
    # Custom board pipeline (see `_parse_board_statuses`). Empty list = fall
    # back to the built-in five-status lifecycle everywhere.
    board_statuses: list = field(default_factory=list)
    # Kanban drag-drop: does dropping a card into in_progress launch a run
    # (today's <select>-driven behavior) or only change status, leaving
    # Launch as an explicit action in the task modal? Default false — a
    # drag should not have a launch side effect unless a project opts in.
    launch_on_drag_in_progress: bool = False
    git_pr_base: str = ""
    git_branch_prefix: str = "harn/"
    git_push_remote: str = "origin"
    intake_confirm_before_run: bool = True
    raw: dict = field(default_factory=dict)

    @property
    def agent_chain(self) -> list[str]:
        """Ordered agents to try (multi-agent list, or the single agent)."""
        if self.agents:
            return self.agents
        return [self.agent]

    @classmethod
    def load(cls, env_dir: Path) -> "Config":
        data = copy.deepcopy(DEFAULTS)
        toml_path = env_dir / "harn.toml"
        if toml_path.exists():
            with open(toml_path, "rb") as fh:
                loaded = _toml.load(fh)
            for section, values in loaded.items():
                data.setdefault(section, {}).update(values)

        agents = [str(a).strip() for a in data["harn"].get("agents", []) if str(a).strip()]
        env_agents = os.environ.get("HARN_AGENTS", "").strip()
        if env_agents:
            agents = [a.strip() for a in env_agents.split(",") if a.strip()]

        return cls(
            agent=os.environ.get("HARN_AGENT", data["harn"]["agent"]),
            agents=agents,
            model=str(os.environ.get("HARN_MODEL") or data["harn"].get("model", "") or "").strip(),
            project=str(data["harn"].get("project", "prj001")).strip().lower(),
            autonomy=_clamp01(
                os.environ.get("HARN_AUTONOMY") or data["harn"].get("autonomy", 0.7)
            ),
            require_mcp=bool(data["harn"].get("require_mcp", True)),
            guidance=str(data["harn"].get("guidance", "lean") or "lean").strip().lower(),
            test_cmd=data["feedback"].get("test_cmd", ""),
            require_tests=bool(data["feedback"].get("require_tests", True)),
            max_iterations=_nonneg_int(data["loop"].get("max_iterations", 10)),
            loop_aware=bool(data["loop"].get("loop_aware", True)),
            code_search_semble=bool(data["code_search"].get("semble", True)),
            code_search_socraticcode=bool(data["code_search"].get("socraticcode", True)),
            mcp_context7=bool(data["mcp"].get("context7", True)),
            auto=bool(data["loop"].get("auto", False)),
            auto_max_iterations=int(data["loop"].get("auto_max_iterations", 30)),
            oracle=bool(data["loop"].get("oracle", False)),
            oracle_agent=str(data["loop"].get("oracle_agent", "") or "").strip(),
            design=bool(data["loop"].get("design", True)),
            auto_reconcile=bool(data["loop"].get("auto_reconcile", True)),
            browser_enabled=bool(data["browser"].get("enabled", False)),
            app_cmd=str(data["browser"].get("app_cmd", "") or "").strip(),
            app_url=str(data["browser"].get("app_url", "") or "").strip(),
            ready_timeout_s=int(data["browser"].get("ready_timeout_s", 60)),
            idle_minutes=int(data["notify"].get("idle_minutes", 30)),
            wait_for_reply=bool(data["notify"].get("wait_for_reply", True)),
            wait_timeout_minutes=int(data["notify"].get("wait_timeout_minutes", 0)),
            hil_channel=_hil_channel(data["notify"].get("channel", "both")),
            chat_grace_minutes=int(
                os.environ.get("HARN_CHAT_GRACE_MINUTES")
                or data["notify"].get("chat_grace_minutes", 2)
            ),
            log_changes=bool(data.get("log", {}).get("changes", True)),
            board_statuses=_parse_board_statuses(data.get("board", {}) or {}),
            launch_on_drag_in_progress=bool(
                (data.get("board", {}) or {}).get("launch_on_drag_in_progress", False)
            ),
            max_cost_usd=_nonneg_float(data["loop"].get("max_cost_usd", 3.0)),
            max_tokens=_nonneg_int(data["loop"].get("max_tokens", 400000)),
            turn_timeout_seconds=_nonneg_int(data["loop"].get("turn_timeout_seconds", 1800)),
            mcp_ui_supervise=bool(data["mcp"].get("ui_supervise", True)),
            mcp_ui_port=_nonneg_int(data["mcp"].get("ui_port", 8765)),
            mcp_tool_reload_seconds=_nonneg_int(data["mcp"].get("tool_reload_seconds", 2)),
            git_pr_base=str((data.get("git", {}) or {}).get("pr_base", "") or "").strip(),
            git_branch_prefix=str((data.get("git", {}) or {}).get("branch_prefix", "harn/") or "harn/").strip(),
            git_push_remote=str((data.get("git", {}) or {}).get("push_remote", "origin") or "origin").strip(),
            intake_confirm_before_run=bool((data.get("intake", {}) or {}).get("confirm_before_run", True)),
            raw=data,
        )
