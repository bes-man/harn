"""Deterministic run-lifecycle state machine (harn_env/state/run.json).

Why this module exists — a real, observed failure, not a hypothetical:

Run liveness used to be inferred from a bare PID file, i.e. `os.kill(pid, 0)`.
That answers "does a process with this id still exist?", which is NOT the same
question as "is a run still making progress?" — and the two diverge in
practice. A `harn run` that had already finished its work hung afterwards in a
Telegram retry loop and kept its pid alive for over an hour. The single-runner
guard therefore refused every new launch, while the board showed the task as
`todo` with every step `pending`: a dead end with no action available to the
human, and nothing that would ever clear it.

The fix has three parts, and all three matter:

1. **Explicit states with a validated transition table.** An unknown or
   illegal transition is rejected instead of silently overwriting the record.
   No more "some writer put the file in a shape no reader expects".

2. **A heartbeat.** A live run stamps `updated_at` as it works — at each step
   boundary AND from inside a turn's event stream, so a long agent turn (six
   minutes is normal) doesn't look stalled. Silence is then genuine evidence.

3. **Reconciliation on every read.** A stored record is never trusted on its
   own: `reconcile()` checks it against the world (is the pid alive? has the
   heartbeat gone stale?) before anyone acts on it. It is a pure function of
   (record, pid-liveness, now) — the same inputs always produce the same
   verdict, so recovery is deterministic rather than "sometimes it fixes
   itself". This is what guarantees the UI can always offer Resume/Restart.

Two independent mechanisms release the lock, deliberately: the run itself
calls `finish()` the moment its work returns (immediate, covers the hang
above, since the hang happened *after* the work), and reconciliation reaps a
record whose process died or went silent (the backstop, covers SIGKILL, a
power cut, or a process wedged mid-work).
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

# ── states ─────────────────────────────────────────────────────────────────
IDLE = "idle"            # nothing has ever run, or the record was cleared
STARTING = "starting"    # process spawned; hasn't reported progress yet
RUNNING = "running"      # actively working, heartbeat fresh
BLOCKED = "blocked"      # stopped on purpose, waiting on a human answer
DONE = "done"            # finished its work
FAILED = "failed"        # finished with an error
STOPPED = "stopped"      # a human stopped it
CRASHED = "crashed"      # process died or went silent (only reconcile writes this)

#: States that hold the single-runner lock. Everything else is terminal and a
#: new launch may proceed — including BLOCKED, which is "waiting on a human",
#: not "occupied".
ACTIVE = frozenset({STARTING, RUNNING})
TERMINAL = frozenset({IDLE, BLOCKED, DONE, FAILED, STOPPED, CRASHED})
STATES = ACTIVE | TERMINAL

#: The legal moves. Every terminal state can start a new run; a run in flight
#: can only move forward to another terminal state (or heartbeat in place).
#: Written out in full rather than derived, so an illegal move is a data
#: question with an obvious answer, not a rule to re-derive at the call site.
_TRANSITIONS: dict[str, frozenset[str]] = {
    IDLE:     frozenset({STARTING}),
    STARTING: frozenset({RUNNING, BLOCKED, DONE, FAILED, STOPPED, CRASHED}),
    RUNNING:  frozenset({RUNNING, BLOCKED, DONE, FAILED, STOPPED, CRASHED}),
    BLOCKED:  frozenset({STARTING}),
    DONE:     frozenset({STARTING}),
    FAILED:   frozenset({STARTING}),
    STOPPED:  frozenset({STARTING}),
    CRASHED:  frozenset({STARTING}),
}

#: How long a run may go without a heartbeat before it is presumed dead.
#: Generous on purpose: a single agent turn legitimately runs for many
#: minutes, and a false "crashed" verdict on a healthy run would kill real
#: work. The pid-liveness check below is what catches the common case
#: promptly; staleness only has to catch a process that is alive but wedged.
DEFAULT_STALE_AFTER_S = 20 * 60


def can_transition(old: str, new: str) -> bool:
    return new in _TRANSITIONS.get(old, frozenset())


@dataclass
class Run:
    """One run's lifecycle record. `current_step` is where the work is now;
    `only_step` is set when the launch targeted a single step."""
    status: str = IDLE
    task_id: str | None = None
    pid: int | None = None
    current_step: str | None = None
    only_step: str | None = None
    auto: bool = False
    rerun: bool = False
    started_at: float | None = None
    updated_at: float | None = None     # heartbeat
    finished_at: float | None = None
    exit_code: int | None = None
    reason: str = ""
    blocked: bool = False

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE

    def to_dict(self) -> dict:
        return asdict(self)


def _path(env_dir: Path) -> Path:
    return env_dir / "state" / "run.json"


def load(env_dir: Path) -> Run:
    """The stored record, or a fresh IDLE one. Never raises: an unreadable or
    corrupt record is treated as "no run", because refusing to answer would
    strand the caller exactly the way the bug this module fixes did."""
    try:
        data = json.loads(_path(env_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Run()
    if not isinstance(data, dict):
        return Run()
    known = {f: data[f] for f in Run.__dataclass_fields__ if f in data}
    run = Run(**known)
    return run if run.status in STATES else Run()


def save(env_dir: Path, run: Run) -> Run:
    """Atomic write — a reader must never observe a half-written record."""
    p = _path(env_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(run.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return run


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


# ── the launch lock ────────────────────────────────────────────────────────
# `begin()` is check-then-write, so two near-simultaneous launches could both
# see "free" and both start. O_CREAT|O_EXCL makes the critical section a real
# one with no extra dependency. A lock left behind by a killed process is
# broken by age — the section only wraps a couple of file writes, so anything
# older than this is debris, not contention.
_LOCK_STALE_S = 30


@contextmanager
def _launch_lock(env_dir: Path):
    lock = env_dir / "state" / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    try:
        for _ in range(50):
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
        yield acquired
    finally:
        if acquired:
            lock.unlink(missing_ok=True)


# ── reconciliation ─────────────────────────────────────────────────────────
def reconcile(env_dir: Path, *, stale_after: float = DEFAULT_STALE_AFTER_S,
              now: float | None = None) -> Run:
    """Return the record as it is *actually* true right now, persisting any
    correction. Pure in its decision: (record, pid liveness, now) fully
    determine the verdict.

    A record claiming to be ACTIVE is only believed while its process is alive
    AND it has spoken recently. Anything else is reaped to CRASHED, which is
    terminal — so the lock is released and the human gets Resume/Restart back.
    """
    run = load(env_dir)
    if not run.is_active:
        return run
    now = time.time() if now is None else now
    # `pid is None` is the legitimate sliver between claiming the slot and the
    # subprocess actually existing. There's nothing to check liveness against
    # yet, so fall through to the staleness rule rather than reaping a run
    # that is still being born.
    if run.pid is not None and not pid_alive(run.pid):
        return _reap(env_dir, run, "the run process exited without reporting a result")
    last = run.updated_at or run.started_at or now
    silent_for = now - last
    if silent_for > stale_after:
        return _reap(env_dir, run,
                     f"no sign of life for {int(silent_for // 60)} min — "
                     "the run process is still up but stopped making progress")
    return run


def _reap(env_dir: Path, run: Run, reason: str) -> Run:
    run.status = CRASHED
    run.reason = reason
    run.finished_at = time.time()
    run.current_step = None
    return save(env_dir, run)


def active(env_dir: Path, *, stale_after: float = DEFAULT_STALE_AFTER_S) -> Run | None:
    """The live run, or None. Always reconciled first — this is the single
    gate every "is something running?" question goes through."""
    run = reconcile(env_dir, stale_after=stale_after)
    return run if run.is_active else None


# ── transitions ────────────────────────────────────────────────────────────
def begin(env_dir: Path, task_id: str, *, pid: int | None = None,
          auto: bool = False, only_step: str | None = None,
          rerun: bool = False,
          stale_after: float = DEFAULT_STALE_AFTER_S) -> dict:
    """Claim the runner for `task_id`. Returns {"ok": True, "run": Run} or
    {"ok": False, "error": ...} when another run genuinely holds it."""
    with _launch_lock(env_dir) as got_lock:
        current = reconcile(env_dir, stale_after=stale_after)
        if current.is_active:
            return {"ok": False, "error":
                    f"a run is already active for {current.task_id} "
                    f"(pid {current.pid}) — stop it first",
                    "run": current}
        if not can_transition(current.status, STARTING):
            return {"ok": False, "error":
                    f"cannot start a run from state {current.status!r}",
                    "run": current}
        now = time.time()
        run = Run(status=STARTING, task_id=task_id, pid=pid, auto=auto,
                  only_step=only_step, rerun=rerun,
                  started_at=now, updated_at=now)
        save(env_dir, run)
        if not got_lock:
            # Degraded but not silent: the section ran unguarded, so say so
            # rather than implying an exclusivity that wasn't enforced.
            return {"ok": True, "run": run,
                    "warning": "launch lock unavailable; started without it"}
        return {"ok": True, "run": run}


#: In-process memo of the last heartbeat write per env, so the high-frequency
#: caller (an agent turn's event stream) can beat unconditionally without
#: turning every streamed token into a disk write. Only the run process itself
#: heartbeats, so a process-local memo is the whole truth.
_last_beat: dict[str, float] = {}


def heartbeat(env_dir: Path, *, pid: int | None = None,
              step_id: str | None = None, throttle_s: float = 0.0) -> Run | None:
    """Stamp "still working" (and optionally which step). No-ops unless the
    stored record is an active run belonging to `pid` (default: this process)
    — a stray heartbeat must never resurrect or hijack someone else's run.

    `throttle_s` skips the write when the last one was that recent; step
    boundaries beat with the default 0 (always record, they're rare and
    meaningful), event streams pass a few seconds.
    """
    pid = os.getpid() if pid is None else pid
    if throttle_s:
        key = str(env_dir)
        now = time.time()
        if now - _last_beat.get(key, 0.0) < throttle_s:
            return None
        _last_beat[key] = now
    run = load(env_dir)
    if not run.is_active or (run.pid and int(run.pid) != int(pid)):
        return None
    if run.status == STARTING and not can_transition(STARTING, RUNNING):
        return None
    run.status = RUNNING
    run.updated_at = time.time()
    _last_beat[str(env_dir)] = run.updated_at
    if step_id is not None:
        run.current_step = step_id
    return save(env_dir, run)


def finish(env_dir: Path, status: str, *, reason: str = "",
           exit_code: int | None = None, pid: int | None = None,
           task_id: str | None = None) -> Run | None:
    """Close the run out. Idempotent and ownership-checked: the run process
    and the reaper thread both call this, and whichever arrives second must
    not reopen or rewrite a record that has already settled.

    `pid`/`task_id`, when given, must match the stored record — this is what
    stops a late finish from a previous run clobbering the run that replaced
    it.
    """
    if status not in TERMINAL:
        raise ValueError(f"not a terminal status: {status!r}")
    run = load(env_dir)
    if pid is not None and run.pid and int(run.pid) != int(pid):
        return None
    if task_id is not None and run.task_id and run.task_id != task_id:
        return None
    if not run.is_active:
        return None
    if not can_transition(run.status, status):
        return None
    run.status = status
    run.reason = reason or run.reason
    run.blocked = status == BLOCKED
    run.exit_code = exit_code
    run.finished_at = time.time()
    run.current_step = None
    return save(env_dir, run)


def clear(env_dir: Path) -> Run:
    """Drop the record entirely (a clean restart's fresh slate)."""
    _path(env_dir).unlink(missing_ok=True)
    return Run()
