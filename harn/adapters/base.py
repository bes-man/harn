from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


# Agent CLIs are routinely installed outside a minimal/GUI-launched process's
# PATH (`claude` in ~/.local/bin, brew shims in /opt/homebrew/bin, npm globals,
# etc.). `harn ui`/`harn run` may be started by launchd, an IDE, or a bare
# shell whose PATH is just /usr/bin:/bin — so plain shutil.which misses a CLI
# that's clearly installed. We search PATH first, then these common dirs, so
# detection (and the studio model dropdown) matches what the user actually has.
_EXTRA_BIN_DIRS = (
    "~/.local/bin", "~/bin", "~/.npm-global/bin",
    "/opt/homebrew/bin", "/usr/local/bin",
    "~/.local/share/claude/bin", "~/.cursor/bin",
)


def resolve_binary(binary: str) -> str | None:
    """Full path to `binary` if runnable, searching PATH then common install
    dirs that a stripped-PATH process would miss. None if not found anywhere."""
    if not binary:
        return None
    hit = shutil.which(binary)
    if hit:
        return hit
    for d in _EXTRA_BIN_DIRS:
        cand = Path(os.path.expanduser(d)) / binary
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


@dataclass
class AgentResult:
    ok: bool
    text: str
    # Token usage for the turn, when the agent CLI reports it (e.g. Claude's
    # --output-format json). None means "this agent didn't expose it".
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def usage_str(self) -> str:
        """Compact 'in+out tokens (~$cost)' for logs, or '' if unknown."""
        if self.total_tokens is None:
            return ""
        s = f"{self.input_tokens or 0}+{self.output_tokens or 0} tokens"
        if self.cost_usd is not None:
            s += f" (~${self.cost_usd:.4f})"
        return s


@dataclass
class _Exec:
    ok: bool
    stdout: str
    stderr: str
    timed_out: bool = False


class Adapter:
    """Base adapter. Subclasses run a single agent turn headless."""

    name = "base"
    binary = ""

    # Per-turn override flags, applied by `_model_args` below. Every adapter
    # defaults to this near-universal CLI convention; if your installed CLI
    # version uses different flags (or doesn't support one at all), override
    # the relevant *_FLAG on the subclass, or set it to None to skip that
    # override entirely for this agent. A flag your CLI doesn't recognize makes
    # the process exit with a visible argument error (shows up in the run's
    # log/trace) — never a silent wrong behavior, so this is a safe default to
    # try even when unconfirmed for a given CLI version.
    MODEL_FLAG: str | None = "--model"
    EFFORT_FLAG: str | None = "--effort"
    TEMPERATURE_FLAG: str | None = "--temperature"

    # Curated "known to exist for this CLI" values, purely so the studio UI can
    # offer a dropdown instead of a blind text box. Best-effort and will drift
    # as providers ship new models — the UI always keeps a free-text "Other…"
    # escape hatch alongside these, so a stale list here degrades to today's
    # behavior (type it yourself) rather than blocking anything.
    MODELS: tuple[str, ...] = ()
    EFFORTS: tuple[str, ...] = ("low", "medium", "high")
    TEMPERATURES: tuple[str, ...] = ("0", "0.2", "0.5", "0.7", "1.0")

    def available(self) -> bool:
        """Whether the underlying CLI/binary is installed and runnable —
        searching PATH plus common install dirs (see resolve_binary)."""
        return resolve_binary(self.binary) is not None

    def _model_args(self, model: str | None = None, effort: str | None = None,
                    temperature: str | None = None) -> list[str]:
        """CLI args for this turn's per-stage overrides (harn_env/harn.toml's
        `[models.<stage>]`), empty for anything not set or not supported by
        this adapter."""
        args: list[str] = []
        if model and self.MODEL_FLAG:
            args += [self.MODEL_FLAG, model]
        if effort and self.EFFORT_FLAG:
            args += [self.EFFORT_FLAG, effort]
        if temperature and self.TEMPERATURE_FLAG:
            args += [self.TEMPERATURE_FLAG, str(temperature)]
        return args

    def run_turn(self, prompt: str, cwd: Path, timeout: int = 1800, *,
                model: str | None = None, effort: str | None = None,
                temperature: str | None = None) -> AgentResult:
        """Run one non-interactive turn with `prompt` in working dir `cwd`.
        `model`/`effort`/`temperature` are this step's optional overrides
        (set per-step in the task's WORKFLOW.md plan)."""
        raise NotImplementedError

    def _exec(self, argv: Sequence[str], cwd: Path, timeout: int) -> _Exec:
        """Run the CLI and return raw streams separately (so a JSON-emitting
        adapter can parse clean stdout)."""
        argv = list(argv)
        # subprocess.run also resolves argv[0] via PATH only — so a CLI found
        # only by available()'s wider search would still fail to launch here.
        # Only when the bare name isn't on PATH do we swap in the resolved
        # absolute path (keeps the common on-PATH case as the plain name).
        if argv and argv[0] == self.binary and shutil.which(self.binary) is None:
            resolved = resolve_binary(self.binary)
            if resolved:
                argv[0] = resolved
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return _Exec(proc.returncode == 0, proc.stdout or "", proc.stderr or "")
        except subprocess.TimeoutExpired:
            return _Exec(False, "", f"{self.name} timed out after {timeout}s", True)

    def _run_cli(
        self, argv: Sequence[str], cwd: Path, timeout: int = 1800
    ) -> AgentResult:
        """Shared headless invocation: shell out to a CLI and capture output.

        Adapters only differ in the argv they build; the subprocess handling
        (missing binary, timeout, merged stdout/stderr) is identical, so it
        lives here once.
        """
        if not self.available():
            return AgentResult(
                ok=False,
                text=f"{self.binary} CLI not found on PATH. Install {self.name} first.",
            )
        r = self._exec(argv, cwd, timeout)
        if r.timed_out:
            return AgentResult(ok=False, text=r.stderr)
        return AgentResult(ok=r.ok, text=r.stdout + r.stderr)
