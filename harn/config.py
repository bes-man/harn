"""Configuration loading for a materialized harn_env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore


DEFAULTS: dict = {
    "harn": {"agent": "claude"},
    "feedback": {"test_cmd": ""},
    "loop": {"max_iterations": 10},
    "notify": {"idle_minutes": 30},
}


@dataclass
class Config:
    agent: str = "claude"
    test_cmd: str = ""
    max_iterations: int = 10
    idle_minutes: int = 30
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, env_dir: Path) -> "Config":
        data = dict(DEFAULTS)
        toml_path = env_dir / "harn.toml"
        if toml_path.exists():
            with open(toml_path, "rb") as fh:
                loaded = _toml.load(fh)
            for section, values in loaded.items():
                data.setdefault(section, {}).update(values)
        return cls(
            agent=os.environ.get("HARN_AGENT", data["harn"]["agent"]),
            test_cmd=data["feedback"].get("test_cmd", ""),
            max_iterations=int(data["loop"].get("max_iterations", 10)),
            idle_minutes=int(data["notify"].get("idle_minutes", 30)),
            raw=data,
        )
