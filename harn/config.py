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


_HIL_CHANNELS = {"chat", "telegram", "both"}


def _hil_channel(value: str) -> str:
    ch = (os.environ.get("HARN_HIL_CHANNEL") or value or "both").strip().lower()
    return ch if ch in _HIL_CHANNELS else "both"


DEFAULTS: dict = {
    # `agents` (a list) is the multi-agent form; `agent` (a string) is the
    # single-agent shorthand kept for back-compat. Either way the agents share
    # all context (AGENTS.md, the task board, PROGRESS.md, ANSWERS.md, MCP), so
    # whichever one runs next understands what's done and what's planned.
    "harn": {"agent": "claude", "agents": [], "project": "prj001"},
    "feedback": {"test_cmd": ""},
    "loop": {"max_iterations": 10, "loop_aware": True, "verify": True,
             "auto": False, "auto_max_iterations": 30,
             "planning": True, "oracle": True, "oracle_agent": ""},
    "code_search": {"semble": True, "socraticcode": True},
    "notify": {"idle_minutes": 30, "wait_for_reply": True, "wait_timeout_minutes": 0,
               "channel": "both", "chat_grace_minutes": 5},
}


@dataclass
class Config:
    agent: str = "claude"
    agents: list[str] = field(default_factory=list)
    project: str = "prj001"   # project code for the prj…-prd…-task… scheme
    test_cmd: str = ""
    max_iterations: int = 10
    loop_aware: bool = True
    verify: bool = True
    auto: bool = False
    auto_max_iterations: int = 30
    # Code search backends (both default on; gracefully degrade if not installed)
    code_search_semble: bool = True       # use semble for semantic chunk retrieval
    code_search_socraticcode: bool = True  # use SocratiCode for dependency graphs
    # Planning turn: first agent turn for a new task asks clarifying questions
    # and writes acceptance criteria before any code is written.
    planning: bool = True
    # Oracle turn: after verify, an independent agent with fresh context checks
    # whether the work actually solves the problem and flags technical debt.
    oracle: bool = True
    oracle_agent: str = ""   # empty = same as main agent
    idle_minutes: int = 30
    wait_for_reply: bool = True
    wait_timeout_minutes: int = 0
    # HIL routing: where a blocking question goes and how long to wait in the
    # chat before escalating to Telegram.
    hil_channel: str = "both"          # chat | telegram | both
    chat_grace_minutes: int = 5        # 0 = escalate to Telegram immediately
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
            project=str(data["harn"].get("project", "prj001")).strip().lower(),
            test_cmd=data["feedback"].get("test_cmd", ""),
            max_iterations=int(data["loop"].get("max_iterations", 10)),
            loop_aware=bool(data["loop"].get("loop_aware", True)),
            verify=bool(data["loop"].get("verify", True)),
            code_search_semble=bool(data["code_search"].get("semble", True)),
            code_search_socraticcode=bool(data["code_search"].get("socraticcode", True)),
            auto=bool(data["loop"].get("auto", False)),
            auto_max_iterations=int(data["loop"].get("auto_max_iterations", 30)),
            planning=bool(data["loop"].get("planning", True)),
            oracle=bool(data["loop"].get("oracle", False)),
            oracle_agent=str(data["loop"].get("oracle_agent", "") or "").strip(),
            idle_minutes=int(data["notify"].get("idle_minutes", 30)),
            wait_for_reply=bool(data["notify"].get("wait_for_reply", True)),
            wait_timeout_minutes=int(data["notify"].get("wait_timeout_minutes", 0)),
            hil_channel=_hil_channel(data["notify"].get("channel", "both")),
            chat_grace_minutes=int(
                os.environ.get("HARN_CHAT_GRACE_MINUTES")
                or data["notify"].get("chat_grace_minutes", 5)
            ),
            raw=data,
        )
