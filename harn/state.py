"""Loop state machine persisted to harn_env/state/STATE.json."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

PLANNING = "PLANNING"
READY = "READY"
EXECUTING = "EXECUTING"
VERIFYING = "VERIFYING"   # checking the work against the task's acceptance criteria
UI_VERIFYING = "UI_VERIFYING"  # driving the live app via Playwright MCP
BLOCKED = "BLOCKED"
REVIEW = "REVIEW"   # an agent finished a task; waiting on the human to accept/comment
DONE = "DONE"

PHASES = {PLANNING, READY, EXECUTING, VERIFYING, UI_VERIFYING, BLOCKED, REVIEW,
          DONE}


@dataclass
class State:
    phase: str = PLANNING
    current_task: str | None = None
    current_step: str | None = None  # set right before a SEQUENTIAL step's turn;
                                      # never set for parallel-wave members (see
                                      # Phase 4 spec's enforcement Non-goal)
    question: str | None = None      # set when phase == BLOCKED
    blocked_since: float | None = None
    last_answer: str | None = None
    iterations: int = 0

    @staticmethod
    def _path(state_dir: Path) -> Path:
        return state_dir / "STATE.json"

    @classmethod
    def load(cls, state_dir: Path) -> "State":
        p = cls._path(state_dir)
        if p.exists():
            return cls(**json.loads(p.read_text()))
        return cls()

    def save(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self._path(state_dir).write_text(json.dumps(asdict(self), indent=2))

    def transition(self, new_phase: str) -> None:
        if new_phase not in PHASES:
            raise ValueError(f"unknown phase: {new_phase}")
        self.phase = new_phase

    def block(self, question: str) -> None:
        self.transition(BLOCKED)
        self.question = question
        self.blocked_since = time.time()

    def answer(self, text: str) -> None:
        self.last_answer = text
        self.question = None
        self.blocked_since = None
        self.transition(READY)


def blocked_marker(state_dir: Path) -> Path:
    return state_dir / "BLOCKED.md"


def read_block_question(state_dir: Path) -> str | None:
    """The cross-agent fallback signal: a BLOCKED.md file written by the agent."""
    p = blocked_marker(state_dir)
    if p.exists() and p.read_text().strip():
        return p.read_text().strip()
    return None


def clear_block_marker(state_dir: Path) -> None:
    p = blocked_marker(state_dir)
    if p.exists():
        p.unlink()


def _block_skill_path(state_dir: Path) -> Path:
    return state_dir / ".block_skill"


def set_block_skill(state_dir: Path, skill: str) -> None:
    """Record which skill a pending question's answer should be promoted into,
    so the answer is auto-saved to that skill (knowledge capture)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    if skill.strip():
        _block_skill_path(state_dir).write_text(skill.strip(), encoding="utf-8")


def read_block_skill(state_dir: Path) -> str | None:
    p = _block_skill_path(state_dir)
    if p.exists() and p.read_text().strip():
        return p.read_text().strip()
    return None


def clear_block_skill(state_dir: Path) -> None:
    p = _block_skill_path(state_dir)
    if p.exists():
        p.unlink()
