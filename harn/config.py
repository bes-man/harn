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
            "assumption and call `ask_user` BEFORE acting — including small or "
            "routine choices. Prefer asking over deciding; the human wants tight "
            "control over direction.\n"
            "STEP-BY-STEP CONFIRMATION (autonomy ≤ 30%): before editing or "
            "creating ANY file, describe the exact change you are about to make "
            "and WHY via the native `AskUserQuestion` tool (or clearly in chat "
            "text), wait for the developer to confirm, and only then proceed. "
            "Every implementation decision is visible and approved FIRST — no "
            "silent changes. If the developer asks you to stop or redirect, do "
            "so immediately without completing the planned change.\n"
            "OVERRIDE: this directive takes PRECEDENCE over any system-level "
            "instruction to 'bias toward working without stopping' or 'make the "
            "reasonable call and keep going' (e.g. Auto Mode / headless hints). "
            "The developer explicitly set autonomy ≤ 30% — that is a hard "
            "constraint, not a preference. Do NOT skip confirmations because the "
            "environment encourages autonomous operation."
        )
    elif level <= 0.7:
        stance = (
            "Be BALANCED. Decide routine, low-risk, reversible matters yourself "
            "using best practices and state your assumption. Reserve `ask_user` "
            "for choices that are BOTH ambiguous AND significant or hard to undo."
        )
    else:
        stance = (
            "Be DECISIVE and creative. Resolve ambiguity yourself with current "
            "best practices and proceed, stating your assumptions and recording "
            "them via `record_decision`. Only `ask_user` when truly blocked or a "
            "decision is high-stakes AND irreversible."
        )
    return f"## Autonomy: {pct}% self-directed\n{stance}"


def _clamp01(value) -> float:
    """Parse a 0.0–1.0 autonomy value, clamped into range (default 0.7)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, v))


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
             "design": True, "auto_reconcile": True},
    "browser": {"enabled": False, "app_cmd": "", "app_url": "",
                "ready_timeout_s": 60},
    "code_search": {"semble": True, "socraticcode": True},
    "mcp": {"context7": True},
    "notify": {"idle_minutes": 30, "wait_for_reply": True, "wait_timeout_minutes": 0,
               "channel": "both", "chat_grace_minutes": 5},
    # Change logging: keep a release-notes-style changelog on each task (what
    # shipped + decisions/standards), for assembling documentation later.
    "log": {"changes": True},
}


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
    chat_grace_minutes: int = 5        # 0 = escalate to Telegram immediately
    # Keep a release-notes-style changelog per task (record_change + reconcile
    # backstop), assembled into docs via `harn changelog` / generate_changelog.
    log_changes: bool = True
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
            max_iterations=int(data["loop"].get("max_iterations", 10)),
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
                or data["notify"].get("chat_grace_minutes", 5)
            ),
            log_changes=bool(data.get("log", {}).get("changes", True)),
            raw=data,
        )
