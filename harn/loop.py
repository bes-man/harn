"""The ralph-style loop, driven by harn (not by each agent's native hooks),
so behavior is identical across Claude / Codex / Cursor / Antigravity.

Per task, the loop walks a transparent, human-visible track:

    todo  →  in_progress  →  review  ⇄  changes_requested  →  done

The agent does ONE focused turn; harn runs the tests and submits the work for
human review. The human accepts it (→ done, with notes for future agents) or
asks for changes (→ the agent reworks it). All context — the task board, the
PROGRESS log, prior answers — is shared through files + the MCP server, so any
agent that runs next picks up with full knowledge of what's done and planned.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import design as design_mod, events, gitutil, progress, \
    prd as prd_mod, semble_bridge, skills, state, tasks, workflows
from .adapters import Adapter, get_adapter
from .config import Config
from .feedback import run_feedback
from .notify import notify
from .telegram import TelegramHIL

# Words (EN + RU) that, as the first token of a review reply, mean "accept".
_APPROVE_WORDS = {
    "approve", "approved", "accept", "accepted", "ok", "okay", "lgtm",
    "ship", "go", "good",
    "принято", "принять", "принимаю", "одобряю", "одобрено", "апрув", "годится",
}


# --------------------------------------------------------------------------- #
# `harn explain` — the ACTIVE workflow's enabled steps (predictability before
# running). The engine walks each task's own plan; this renders the project
# default (WORKFLOW.md) so you can SEE what will run before running it.
# --------------------------------------------------------------------------- #
def explain(env_dir: Path, cfg: Config) -> str:
    """Human-readable view of the ACTIVE workflow — its enabled steps, in order,
    with each agent step's agent/model override (or the run default), and each
    command step's shell command. Powers `harn explain`."""
    from . import workflow as workflow_mod
    parsed = workflow_mod.parse(env_dir)
    steps = [n for n in parsed.get("nodes", []) if n.get("kind") == "step"]
    default_agent = cfg.agent_chain[0] if cfg.agent_chain else "?"
    default_model = cfg.model or "default"
    lines = ["Workflow for this task (todo → in_progress → review → done):"]
    n = 0
    for s in steps:
        on = s.get("enabled", True) is not False
        mark = "✓" if on else "·"
        n += 1
        if s.get("type") == "command":
            detail = f"(command: {s.get('command', '').strip() or '?'})"
        else:
            agent = (s.get("agent") or "").strip() or default_agent
            model = (s.get("model") or "").strip() or default_model
            detail = f"({agent} / {model})"
        lines.append(f"  {n}. [{mark}] {s.get('title', '')} {detail}")
    if n == 0:
        lines.append("  (no steps defined in WORKFLOW.md)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Agent selection (multi-agent: try the configured chain, use the first ready)
# --------------------------------------------------------------------------- #
def _pick_adapter(cfg: Config) -> Adapter:
    chain = cfg.agent_chain
    for name in chain:
        ad = get_adapter(name)
        if ad.available():
            return ad
    # None installed: return the first so run_turn yields a helpful error.
    return get_adapter(chain[0])


# --------------------------------------------------------------------------- #
# Prompt assembly — loop-aware so the agent always sees the whole picture
# --------------------------------------------------------------------------- #
_ASK_GUIDANCE = (
    "When you call `ask_user`, write the question EXPANDED so the human can "
    "decide fast: (1) the context and WHY the question arose, (2) the concrete "
    "options with each one's trade-off, (3) your recommended option and a "
    "one-line reason. Never ask a bare one-liner, and never guess."
)

# Injected in --auto runs: the agent decides for itself instead of asking.
_AUTO_NOTE = (
    "## AUTONOMOUS MODE — no human is available\n"
    "Do NOT call `ask_user` or write `harn_env/state/BLOCKED.md`. For any "
    "ambiguity, research comprehensive, current best practices, weigh the "
    "options, choose the optimal one, and state your assumption EXPLICITLY in "
    "your reply before proceeding. Do NOT modify any .md files under "
    "`harn_env/` (task files, PROGRESS, notes) — change code only. Work as "
    "carefully and consciously as you can; this is an unattended pass."
)

# Injected into agent-turn steps that are part of a parallel wave: the sibling
# steps are running concurrently in their OWN isolated worktree copies, so
# ask_user (which would stall the whole wave for one thread) is discouraged.
_PARALLEL_NOTE = (
    "## You are one of several PARALLEL steps running right now\n"
    "Other steps in this wave are running CONCURRENTLY in their own isolated "
    "copies of the repo — you cannot see their in-progress changes, and they "
    "cannot see yours, until this wave finishes and merges. Avoid `ask_user` "
    "unless truly blocked: decide autonomously using current best practices "
    "and record your assumption via `record_decision` so it can be reviewed."
)

# Feedback fed back to the agent after it tries to block in --auto mode.
_AUTO_DECIDE_NOTE = (
    "AUTONOMOUS: you raised a question but no human is available. Research the "
    "best practice, decide, state your assumption, and proceed — do not ask."
)

# Fed back when the human presses the Telegram "Decide for me" (Auto) button.
_AUTO_BUTTON_NOTE = (
    "The human chose 'Decide for me' on your question. Pick the best option "
    "yourself using current best practices, state your assumption clearly, and "
    "proceed. Record the choice with `record_decision` so it can be reviewed."
)

# Standing "save your learnings" nudge appended to every step's prompt.
_INSIGHT_NUDGE = (
    "## Capture what you learned\n"
    "If this step surfaced a durable convention, trade-off resolution, or "
    "release-worthy change, record it before finishing: `save_to_skill` for "
    "confident conventions, `save_service` for changed service standards, "
    "`record_change` for the release-note line. Skip silently if nothing "
    "durable was learned — do not invent insights."
)


def _autonomy_note(level: float) -> str:
    """Translate the 0.0–1.0 autonomy level into a behavioural directive."""
    from .config import autonomy_directive
    return autonomy_directive(level)

def _design_block(env_dir: Path, task_id: str, max_chars: int = 4000) -> str:
    """The approved mockup, injected into executor/oracle/UI-verify prompts."""
    html = design_mod.load(env_dir, task_id)
    if not html:
        return ""
    rel = design_mod.design_path(env_dir, task_id)
    return (
        f"## Approved UI design (visual contract) — {rel.name}\n"
        "The human approved this mockup during planning. The implemented "
        "interface must match its layout, elements, and states; deviations need "
        "a recorded decision or a new `ask_user`.\n"
        f"```html\n{html[:max_chars]}\n```"
    )


def _oracle_instructions(diff: str, cfg: Config) -> str:
    """Build oracle instructions with diff-scoped blast-radius guidance."""
    blast = semble_bridge.oracle_hint(cfg, diff)
    blast_section = f"\n\n{blast}\n" if blast else ""
    return f"""\
## ORACLE REVIEW — independent verification

You are reviewing completed work. You have NO history of how it was built.
Your scope: the git diff below and its ripple effects — nothing outside that.
{blast_section}
1. **Blast radius**: identify every file/function the diff touches or calls into. \
Focus your review there — do not audit unrelated code.
2. **Check each acceptance criterion** in `## Done when` — is it GENUINELY met? \
Not just superficially. Check the actual implementation, not just the tests.
3. **Correctness gaps**: edge cases, error paths, security issues, missing \
logic that tests don't cover.
4. **Technical debt introduced**: shortcuts, missing abstraction, hardcoded \
values, duplicated logic — things that will cause problems in future iterations.

End your review with EXACTLY one verdict on its own line:

    ORACLE: PASS                    — all criteria met, no significant issues
    ORACLE: FAIL — <reason>         — a criterion is not met; be specific
    ORACLE: DEBT — <description>    — correct but notable debt (surfaces to human)
"""


def _oracle_verdict(text: str) -> tuple[str, str]:
    """Return (verdict, detail) where verdict is PASS / FAIL / DEBT."""
    m = re.search(
        r"ORACLE:\s*(PASS|FAIL|DEBT)(?:\s*[—–-]\s*(.+))?",
        text or "", re.IGNORECASE,
    )
    if not m:
        return ("PASS", "")          # no explicit verdict → assume pass
    verdict = m.group(1).upper()
    detail = (m.group(2) or "").strip()
    return (verdict, detail)


def _git_diff(project_root: Path, max_chars: int = 6000) -> str:
    """Return a summary of uncommitted + recent committed changes, best-effort."""
    try:
        r = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=project_root, capture_output=True, text=True, timeout=10,
        )
        diff = r.stdout.strip() if r.returncode == 0 else ""
        if not diff:                  # all committed — show last commit diff
            r2 = subprocess.run(
                ["git", "diff", "HEAD~1", "HEAD"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            diff = r2.stdout.strip() if r2.returncode == 0 else ""
        return diff[:max_chars]
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Test-writing gate: code changed but no tests did → one nudge per task
# --------------------------------------------------------------------------- #
_CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".kt", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift",
    ".vue", ".svelte",
}
_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/"      # tests/ test/ __tests__/ spec/
    r"|(^|/)test_[^/]+$"                  # test_foo.py
    r"|(_test|\.test|\.spec)\.[^/.]+$",   # foo_test.go foo.test.ts foo.spec.js
    re.IGNORECASE,
)

_TESTS_NUDGE = (
    "Your change modifies code but adds/changes NO tests. Every code change "
    "needs test coverage: write tests that pin down the new behaviour (one per "
    "acceptance criterion where possible), make them pass, then finish the task. "
    "If this change genuinely cannot be tested, record WHY with `record_decision`."
)


def _changed_files(project_root: Path) -> list[str]:
    """Files changed vs HEAD (falling back to the last commit), best-effort."""
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=project_root, capture_output=True, text=True, timeout=10,
        )
        files = r.stdout.split() if r.returncode == 0 else []
        if not files:
            r2 = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            files = r2.stdout.split() if r2.returncode == 0 else []
        return files
    except Exception:
        return []


def _missing_tests(project_root: Path) -> bool:
    """True when the working diff touches code files but no test files."""
    files = _changed_files(project_root)
    code = [f for f in files
            if Path(f).suffix.lower() in _CODE_EXTS
            and not _TEST_FILE_RE.search(f)]
    tests_touched = any(_TEST_FILE_RE.search(f) for f in files)
    return bool(code) and not tests_touched


def _build_oracle_prompt(
    env_dir: Path, cfg: Config, task: tasks.Task, diff: str
) -> str:
    task_body = _task_spec(task)

    # Oracle instructions are diff-aware: include blast-radius guidance scoped
    # to exactly the files that changed (SocratiCode first, semble fallback).
    # AGENTS.md is intentionally omitted: oracle has its own focused instructions
    # and doesn't need the full agent protocol (~1.3k tokens saved per oracle turn).
    parts: list[str] = [_HARN_TOOLS_HINT, _oracle_instructions(diff, cfg)]
    if task.prds:
        prd_parts = []
        for pid in task.prds:
            p = prd_mod.find(env_dir, pid)
            if p:
                prd_parts.append(_prd_excerpt(p, _PRD_INLINE_MAX))
        if prd_parts:
            parts.append("## Requirements context (PRDs)\n" +
                         "\n\n---\n\n".join(prd_parts))
    parts.append(f"## Task specification — {task.id}\n" + task_body)
    dz = _design_block(env_dir, task.id)
    if dz:
        parts.append(dz)
        shots = env_dir / "state" / "screenshots" / task.id
        if shots.is_dir() and any(shots.iterdir()):
            parts.append(
                "## UI evidence\nScreenshots from the browser-verification pass "
                f"are in `{shots}`. Compare them against the approved design "
                "above — a UI that diverges from the contract is grounds for "
                "`ORACLE: FAIL`."
            )
    claims = _decisions_to_verify(task)
    if claims:
        parts.append(claims)
    if diff:
        parts.append(
            "## Git diff (your search scope)\n```diff\n" + diff + "\n```"
        )
    else:
        parts.append(
            "## No diff available\n"
            "Read the files and skills relevant to this task directly."
        )
    return "\n\n".join(p for p in parts if p.strip())


# PRD char cap: injected inline to avoid 10k+ token blowout on large specs.
# The agent calls read_prd() for the remainder — one on-demand fetch beats
# injecting the whole doc into every turn.
_PRD_INLINE_MAX = 2000   # chars in execution / oracle / UI-verify prompts
_PRD_PLAN_MAX = 4000     # slightly more in planning (agent needs context to ask qs)


def _prd_excerpt(prd, max_chars: int) -> str:
    """One PRD's inline excerpt: title + body up to max_chars + truncation note."""
    body = prd.raw.strip()
    if len(body) <= max_chars:
        return f"### PRD: {prd.id} — {prd.title}\n\n{body}"
    cut = body[:max_chars].rsplit("\n", 1)[0]   # break at a newline when possible
    remaining = len(body) - len(cut)
    return (
        f"### PRD: {prd.id} — {prd.title}\n\n{cut}\n\n"
        f"… +{remaining} chars truncated. Call `read_prd(\"{prd.id}\")` for the full text."
    )


# Minimal tool hint injected into specialized turns (verify/oracle/reconcile)
# instead of the full AGENTS.md — saves ~1.3k tokens per specialized turn.
_HARN_TOOLS_HINT = (
    "## harn MCP tools available in this turn\n"
    "`ask_user(question, skill=…)` — ask the developer (BLOCKED until answered). "
    "`answer_question(answer)` — record a human reply. "
    "`save_to_skill(skill, content)` — persist a learning. "
    "`run_tests()` — run the test suite. "
    "`board()` — current task statuses."
)


_LIFECYCLE_NOTE = (
    "## How harn runs (you are one turn of a loop)\n"
    "harn picks the top task and you do ONE focused turn. Then harn runs the "
    "project's tests, VERIFIES your work against the task's acceptance criteria, "
    "runs a RECONCILE turn to capture learnings into skills/standards, then "
    "submits it for HUMAN review and runs an independent ORACLE agent. The "
    "reviewer either accepts it (→ `done`) or replies with changes — in which "
    "case the task comes back as `changes_requested`. Use the harn MCP tools "
    "(`get_next_task`, `read_skill`, `run_tests`, `board`, `submit_for_review`). "
    "If anything is ambiguous or risky, call `ask_user` "
    "(or write `harn_env/state/BLOCKED.md`) and STOP — do not guess.\n"
    "BEFORE any code, run the pre-task protocol from AGENTS.md (AS IS → TO BE → "
    "skills, named → best practices → clarify). Focus your execution turn on "
    "implementation — harn's reconcile turn captures the learnings automatically.\n"
    + _ASK_GUIDANCE
)


def _answers_tail(state_dir: Path, limit: int = 1500) -> str:
    p = state_dir / "ANSWERS.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")[-limit:].strip()


def _task_spec(task: tasks.Task) -> str:
    """Render the task's *specification* (the source of truth) — title, meta,
    description/acceptance criteria, subtasks. Deliberately EXCLUDES runtime
    fields (scratchpad, decisions, review_log) so they never masquerade as
    requirements. Those are injected separately, with the right framing."""
    meta = [f"status: {task.status}", f"priority: {task.priority}"]
    if task.prds:
        meta.append("prds: " + ", ".join(task.prds))
    if task.epic:
        meta.append(f"epic: {task.epic}")
    if task.user_story:
        meta.append(f"user_story: {task.user_story}")
    parts = [f"# {task.title}  ({task.id})", " · ".join(meta)]
    if task.description.strip():
        parts.append(task.description.strip())
    if task.subtasks:
        lines = ["## Subtasks"]
        for s in task.subtasks:
            mark = "x" if s.status == tasks.DONE else " "
            lines.append(f"- [{mark}] {s.title}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _review_history(task: tasks.Task) -> str:
    """Render the review log so the executor can see prior reviewer comments
    (especially for a `changes_requested` rework)."""
    if not task.review_log:
        return ""
    lines = ["## Review history"]
    for e in task.review_log:
        detail = e.comment or e.notes or e.summary or ""
        who = e.by or e.agent or ""
        suffix = f" — {detail}" if detail else ""
        who_s = f" [{who}]" if who else ""
        lines.append(f"- {e.ts} {e.event}{who_s}{suffix}")
    return "\n".join(lines)


def _continuity_block(task: tasks.Task) -> str:
    """Lightweight continuity for the executor/verifier: the note it left itself
    and the decisions it already made (so it stays consistent without re-deriving
    everything — variant 2)."""
    parts: list[str] = []
    if task.scratchpad:
        parts.append(
            "## Your note from the last iteration (continuity — not a requirement)\n"
            + task.scratchpad
        )
    if task.decisions:
        lines = [
            "## Decisions you've already made (stay consistent; refine if wrong)"
        ]
        for d in task.decisions:
            why = f" — {d.rationale}" if d.rationale else ""
            lines.append(f"- {d.decision}{why}")
        lines.append(
            "Keep working from these unless new information shows one is wrong; "
            "if so, change it and call `record_decision` again with the reason."
        )
        parts.append("\n".join(lines))
    if parts:
        parts.append(
            "Use `set_scratchpad` to update your note and `record_decision` for "
            "any new choice, so your next iteration continues smoothly."
        )
    return "\n\n".join(parts)


def _decisions_to_verify(task: tasks.Task) -> str:
    """For the oracle: the agent's decisions presented as CLAIMS to verify —
    explicitly NOT as part of the task's requirements."""
    if not task.decisions:
        return ""
    lines = [
        "## Decisions the agent CLAIMS it made — VERIFY each, do not assume",
        "These are the executor's own choices, not requirements from the human. "
        "For EACH one, check it against the acceptance criteria and the PRD:",
    ]
    for d in task.decisions:
        why = f" (claimed reason: {d.rationale})" if d.rationale else ""
        lines.append(f"- {d.decision}{why}")
    lines.append(
        "If a decision contradicts the requirements, weakens correctness/security, "
        "or its rationale doesn't hold up — that is grounds for `ORACLE: FAIL`. "
        "Do NOT treat the agent's scratchpad or rationale as a spec."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Token accounting (best-effort: only agents that report usage, e.g. Claude)
# --------------------------------------------------------------------------- #
def _accumulate(totals: dict, costs: dict, task_id: str, r) -> None:
    if r.total_tokens is not None:
        totals[task_id] = totals.get(task_id, 0) + r.total_tokens
    if r.cost_usd is not None:
        costs[task_id] = costs.get(task_id, 0.0) + r.cost_usd


def _step_overrides(cfg: Config, step: dict) -> dict:
    """This step's {model, effort, temperature} kwargs for adapter.run_turn.
    The step's own fields win; a missing model falls back to the global
    default ([harn] model). Empty values are omitted entirely."""
    ov = {}
    for key in ("model", "effort", "temperature"):
        v = str(step.get(key) or "").strip()
        if v:
            ov[key] = v
    if "model" not in ov and cfg.model:
        ov["model"] = cfg.model
    return ov


def _adapter_for_step(cfg: Config, step: dict, default: Adapter) -> Adapter:
    """The adapter that runs this step: its `Agent:` override if set and
    known, else the run's default. Provider-agnostic — any step can run on
    any installed CLI."""
    name = str(step.get("agent") or "").strip()
    if not name or name == default.name:
        return default
    try:
        return get_adapter(name)
    except ValueError:
        return default


def _build_step_prompt(env_dir: Path, cfg: Config, task: tasks.Task,
                       step: dict, feedback_tail: str = "",
                       auto: bool = False, onfail_context: str = "",
                       parallel_note: str = "") -> str:
    """ONE prompt builder for EVERY workflow step (replaces the six
    stage-specific builders). Structure is stable
    context first (AGENTS.md, skills index, task spec), the step's own
    instructions in the middle, volatile tail (board/progress/feedback)
    last for prompt-cache reuse."""
    state_dir = env_dir / "state"
    agents_md = env_dir.parent / "AGENTS.md"
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    parts: list[str] = [base]
    if auto:
        parts.append(_AUTO_NOTE)
    if cfg.loop_aware:
        parts.append(_LIFECYCLE_NOTE)
    parts.append(
        "## Available skills (load only what you need)\n"
        "Read a skill via `read_skill` ONLY when needed:\n" + skills.index(env_dir))
    req = [s for s in (step.get("required") or []) if s]
    if req:
        parts.append("## Required skills for THIS step\nLoad these now via "
                     "`read_skill`: " + ", ".join(req))
    parts.append(f"## Current task — {task.id} (status: {task.status})\n"
                 + _task_spec(task))
    tools = [t for t in (step.get("tools") or []) if t]
    parts.append(
        f"## THIS STEP: {step.get('title', '')}\n"
        + (step.get("body") or "").strip()
        + ("\n\nTools for this step: " + ", ".join(tools) if tools else "")
        + "\n\nDo ONLY this step's work, then end your turn — the next step "
          "runs as a separate session with this task's updated state.")
    if onfail_context:
        parts.append(onfail_context)
    if parallel_note:
        parts.append(parallel_note)
    cont = _continuity_block(task)
    if cont:
        parts.append(cont)
    if not auto:
        parts.append(_autonomy_note(cfg.autonomy))
        parts.append("## Rules\n- " + _ASK_GUIDANCE)
    parts.append(_INSIGHT_NUDGE)
    # volatile tail — keep last (prompt cache)
    if cfg.loop_aware:
        parts.append("## Task board\n" + tasks.board(env_dir))
        prog = progress.tail(env_dir)
        if prog:
            parts.append("## Progress so far\n" + prog)
        answers = _answers_tail(state_dir)
        if answers:
            parts.append("## Earlier answers from the human\n" + answers)
    if feedback_tail:
        parts.append("## Last feedback (tests)\n```\n" + feedback_tail + "\n```")
    return "\n\n".join(p for p in parts if p.strip())


def _checkpoint_stage(project_root: Path, task: "tasks.Task", stage: str) -> None:
    """Snapshot the working tree right before this stage's turn runs (see
    gitutil.checkpoint) so the studio UI's per-step Rerun control — or
    `harn run --task ID --stage STAGE --rerun` — can restore EXACTLY this
    starting point later, undoing only what that attempt changed.

    Best-effort: no git repo means no checkpoint, never blocks the turn from
    running (matches gitutil's whole degrade-gracefully philosophy)."""
    ref = gitutil.checkpoint(project_root, task.id, stage)
    if ref:
        task.stage_checkpoints[stage] = ref
        tasks._save(task)


def _run_command_step(env_dir: Path, project_root: Path, task: "tasks.Task",
                      step: dict, steps: list[dict], cfg: Config, adapter) -> str:
    """Run a `type: command` step's shell command (harn/feedback.py's
    run_feedback) instead of an agent turn. Unlike agent-turn steps, a
    command step's ledger entry is written UNCONDITIONALLY — even in `auto`
    mode — because a command's exit code is an objective fact being recorded
    for the next step to see, not the kind of agent-judgment bookkeeping
    `auto` mode is designed to keep out of the .md/JSON files.

    On failure, dispatches the step's `on_fail` handler (if it resolves to a
    live agent step) via `_run_onfail_handler`, then returns so the SAME
    command step is naturally re-selected by `run()`'s `pending[0]`
    reselection next iteration (its ledger status stays "failed", not "ok").

    Returns one of: "advance" (success, or failure with no usable handler —
    either way move on), "handled" (failure dispatched to a handler and
    ledgered; caller should retry), "blocked" (the handler itself blocked on
    ask_user; caller should end the run).
    """
    sid = step.get("id") or ""
    title = step.get("title", "")
    command = step.get("command", "")
    _checkpoint_stage(project_root, task, sid)
    started = tasks._now_iso()
    task.step_results[sid] = {"status": "running", "started": started, "ended": None}
    tasks._save(task)
    # stage_start/stage_end mirror _run_turn's event pair so the studio's
    # progress view (keyed generically by `stage` = step id) paints a command
    # step's live status exactly like an agent step, with zero changes needed
    # on the reading side.
    events.emit(env_dir, "stage_start", task_id=task.id, stage=sid,
                agent="command", step_title=title)
    t0 = time.time()
    result = run_feedback(command, project_root)
    dur_ms = int((time.time() - t0) * 1000)
    ended = tasks._now_iso()
    lines = [ln for ln in (result.output or "").strip().splitlines() if ln.strip()]
    events.emit(env_dir, "stage_end", task_id=task.id, stage=sid,
                agent="command", step_title=title, ok=result.ok, dur_ms=dur_ms,
                summary=(lines[-1][:200] if lines else None))
    if result.ok:
        task.step_results[sid] = {"status": "ok", "started": started,
                                  "ended": ended, "output": result.tail(40)}
        tasks._save(task)
        progress.log(env_dir, f"{task.id}: {title} (command) — ok")
        return "advance"
    task.step_results[sid] = {"status": "failed", "started": started,
                              "ended": ended, "output": result.tail(40)}
    tasks._save(task)
    progress.log(env_dir, f"{task.id}: {title} (command) — failed")
    on_fail_id = str(step.get("on_fail") or "").strip()
    handler = next((s for s in steps if s.get("id") == on_fail_id), None) \
        if on_fail_id else None
    if handler is None or handler.get("type") == "command":
        if on_fail_id:
            events.emit(env_dir, "config_error", task_id=task.id, stage=sid,
                        detail=f"on_fail target {on_fail_id!r} is not a live agent step")
        return "advance"   # no usable handler — record failure, move on (Phase-1-equivalent)
    # Lazily resolve a default adapter here (not eagerly at the caller) —
    # callers that never hit a live on_fail handler (the common case) should
    # never have to resolve or even validate one.
    adapter = adapter or _pick_adapter(cfg)
    return _run_onfail_handler(env_dir, project_root, task, step, handler, cfg, adapter)


def _run_onfail_handler(env_dir: Path, project_root: Path, task: "tasks.Task",
                        failing_step: dict, handler: dict, cfg: Config, adapter) -> str:
    """Dispatch a command step's `on_fail` target as ONE agent turn, reusing
    the same machinery `run()`'s main branch uses for agent steps. The
    handler's prompt gets an extra context block describing the failure
    (`_build_step_prompt`'s `onfail_context` param).

    The handler gets exactly one attempt per failure — no internal retry
    loop. `_handle_block`'s "resumed"/"auto" outcomes normally mean "re-run
    the same step" in `run()`'s main loop, but here any non-"blocked"
    outcome means "this handler attempt is finished": if the human answered
    a question mid-handler, that answer is already recorded in state, and
    the NEXT top-level `run()` iteration re-evaluates `pending` fresh
    (naturally retrying the original failing command step, not the handler).
    """
    handler_adapter = _adapter_for_step(cfg, handler, adapter)
    hid = handler.get("id") or ""
    result_entry = task.step_results.get(failing_step.get("id") or "", {})
    onfail_context = (
        f"## Triggered by a failed step\n**{failing_step.get('title', '')}** "
        f"failed:\n```\n{result_entry.get('output', '')}\n```")
    _checkpoint_stage(project_root, task, hid)
    tok_totals: dict = {}
    tok_costs: dict = {}
    prompt = _build_step_prompt(env_dir, cfg, task, handler,
                                onfail_context=onfail_context)
    started = tasks._now_iso()
    result = _run_turn(handler_adapter, env_dir, prompt, project_root,
                       task_id=task.id, stage=hid, step_title=handler.get("title", ""),
                       overrides=_step_overrides(cfg, handler),
                       tok_totals=tok_totals, tok_costs=tok_costs, cfg=cfg)
    ended = tasks._now_iso()
    state_dir = env_dir / "state"
    st = state.State.load(state_dir)
    b = _handle_block(env_dir, cfg, st, state_dir, task, auto=False)
    if b == "blocked":
        return "blocked"
    # resumed / auto / no-block: handler is DONE either way for this pass —
    # its own ledger entry records the attempt, then the engine retries
    # the original failing command step (NOT the handler) next iteration.
    task.step_results[hid] = {"status": "ok" if result.ok else "failed",
                              "started": started, "ended": ended,
                              "tokens": result.total_tokens}
    tasks._save(task)
    return "handled"


def _run_turn(adapter, env_dir: Path, prompt: str, project_root: Path, *,
              task_id: str, stage: str, tok_totals: dict, tok_costs: dict,
              cfg: Config, verdict: str | None = None,
              overrides: dict | None = None, step_title: str | None = None):
    """Run one agent turn and emit a structured stage_start/stage_end pair.

    Centralising the run_turn call guarantees that EVERY completed agent cycle
    leaves a durable, machine-readable record (tokens, duration, one-line
    summary) in events.jsonl — the observability spine. `_accumulate` is folded
    in so callers keep their running token tally. Re-raises after logging an
    `error` event so the loop's own handling is unchanged.

    `stage` is the step id (the workflow node's stable key); `step_title` is
    that step's human title, emitted alongside so the trace reads clearly.
    `overrides` are the run_turn kwargs (model/effort/temperature) for this
    step — the caller passes `_step_overrides(cfg, step)`.
    """
    events.emit(env_dir, "stage_start", task_id=task_id, stage=stage,
                agent=adapter.name, step_title=step_title)
    t0 = time.time()
    try:
        res = adapter.run_turn(prompt, project_root, **(overrides or {}))
    except Exception as e:
        events.emit(env_dir, "error", task_id=task_id, stage=stage,
                    detail=str(e)[:300])
        raise
    dur_ms = int((time.time() - t0) * 1000)
    _accumulate(tok_totals, tok_costs, task_id, res)
    lines = [ln for ln in (res.text or "").strip().splitlines() if ln.strip()]
    events.emit(env_dir, "stage_end", task_id=task_id, stage=stage,
                agent=adapter.name, ok=res.ok, step_title=step_title,
                tok_in=res.input_tokens, tok_out=res.output_tokens,
                cost_usd=res.cost_usd, dur_ms=dur_ms, verdict=verdict,
                summary=(lines[-1][:200] if lines else None))
    return res


def _usage_summary(totals: dict, costs: dict, task_id: str) -> str:
    t, c = totals.get(task_id), costs.get(task_id)
    if t is None and c is None:
        return ""
    s = f"{t or 0} tokens"
    if c is not None:
        s += f" (~${c:.4f})"
    return s


# --------------------------------------------------------------------------- #
# Human-in-the-loop over Telegram (shared by the block path and the review gate)
# --------------------------------------------------------------------------- #
def _telegram_wait(env_dir: Path, cfg: Config, text: str) -> str | None:
    """Post `text` to Telegram and block for the human's reply, if enabled.

    Returns the reply, or None when disabled / unconfigured / timed out (the
    caller then falls back to the CLI path). Survives the computer sleeping:
    Telegram retains updates, so the long-poll resumes on wake.
    """
    if not cfg.wait_for_reply:
        return None
    tg = TelegramHIL.from_env()
    if tg is None:
        return None
    return tg.wait_for_reply(
        text,
        state_dir=env_dir / "state",
        timeout_s=cfg.wait_timeout_minutes * 60,
        remind_every_s=cfg.idle_minutes * 60,
    )


def _await_answer(
    env_dir: Path, cfg: Config, task_id: str, question: str
) -> tuple[str | None, str]:
    """Wait for the answer across chat and Telegram, escalating after the chat
    grace. Returns `(reply, source)`:
      ('text', 'telegram') — replied in Telegram (caller records it);
      (None,  'chat')      — answered locally while waiting (already recorded);
      (None,  '')          — not waiting / unconfigured / timed out.

    The "chat" channel is detected by the BLOCKED marker being cleared — i.e.
    someone ran `harn answer` or the in-chat agent called the `answer` tool.
    """
    if not cfg.wait_for_reply:
        return (None, "")
    state_dir = env_dir / "state"
    tg = TelegramHIL.from_env()
    # chat-only, or Telegram not configured → just poll for a local answer.
    if cfg.hil_channel == "chat" or tg is None:
        if cfg.hil_channel != "chat" and cfg.wait_for_reply:
            print("[harn] wait_for_reply is on but Telegram is not configured "
                  "(set HARN_TELEGRAM_BOT_TOKEN / HARN_TELEGRAM_CHAT_ID).")
        return (None, "")

    grace = cfg.chat_grace_minutes * 60 if cfg.hil_channel == "both" else 0
    if grace:
        print(f"[harn] '{task_id}': waiting {cfg.chat_grace_minutes}m in chat "
              "before escalating to Telegram…")
    else:
        print(f"[harn] '{task_id}': posting to Telegram and waiting…")
    return tg.await_answer(
        question,
        state_dir=state_dir,
        task_id=task_id,
        timeout_s=cfg.wait_timeout_minutes * 60,
        remind_every_s=cfg.idle_minutes * 60,
        pre_grace_s=grace,
        local_check=lambda: state.read_block_question(state_dir) is None,
    )


def _handle_block(
    env_dir: Path, cfg: Config, st: "state.State", state_dir: Path,
    task: tasks.Task, auto: bool = False,
) -> str:
    """If the agent blocked on a question this turn, handle it. Returns:
       'none'     — no block;
       'auto'     — --auto: cleared the question, agent must decide itself;
       'resumed'  — answered (Telegram or chat); caller reloads state, continues;
       'blocked'  — caller should return the BLOCKED phase.
    """
    question = state.read_block_question(state_dir)
    if not question:
        return "none"
    events.emit(env_dir, "block", task_id=task.id,
                summary=question.splitlines()[0][:200])
    if auto:
        # No human: drop the question and let the agent decide next turn.
        state.clear_block_marker(state_dir)
        print(f"[auto] '{task.id}' raised a question; deciding autonomously.")
        return "auto"
    st.block(question)
    st.save(state_dir)
    reply, source = _await_answer(env_dir, cfg, task.id, question)
    if source == "telegram" and reply is not None:
        answer(env_dir, reply)
        print(f"[harn] Answer received via Telegram; resuming '{task.id}'.")
        return "resumed"
    if source == "chat":
        print(f"[harn] Answered in chat; resuming '{task.id}'.")
        return "resumed"
    if source == "auto":
        # Human pressed "Decide for me" — record the delegation as the answer so
        # the agent sees it and chooses the best option itself.
        answer(env_dir, "You pressed 'Decide for me'. " + _AUTO_BUTTON_NOTE)
        print(f"[harn] Decision delegated to the agent; resuming '{task.id}'.")
        return "resumed"
    channels = notify(f"[harn] Agent needs your input on '{task.id}':\n{question}")
    print(f"[harn] BLOCKED. Notified: {channels or 'none configured'}")
    return "blocked"


# --------------------------------------------------------------------------- #
# Review gate
# --------------------------------------------------------------------------- #
def _parse_review_reply(text: str) -> tuple[str, str]:
    """Map a free-text reply to a decision.

    'approve [notes...]' / 'ok' / 'принято …'  → ("accept", notes)
    anything else                               → ("changes", full text)
    """
    stripped = text.strip()
    if not stripped:
        return "changes", ""
    first, _, rest = stripped.partition(" ")
    token = first.strip().strip(":,.!").lower()
    if token in _APPROVE_WORDS:
        return "accept", rest.strip()
    return "changes", stripped


def _review_request_text(task: tasks.Task, agent_output: str) -> str:
    excerpt = (agent_output or "").strip()[-1200:]
    return (
        f"👀 Review needed — [{task.id}] {task.title}\n\n"
        f"What the agent reports:\n{excerpt or '(no output)'}\n\n"
        "Reply 'approve' (optionally with notes for future agents) to accept, "
        "or describe the changes you want."
    )


def _review_gate(
    env_dir: Path, cfg: Config, task: tasks.Task, agent_output: str
) -> str:
    """Resolve a task in review. Returns 'accepted', 'changes', or 'pending'.

    'pending' means no auto channel resolved it — the task stays in `review`
    for a later `harn review` from the CLI.
    """
    reply = _telegram_wait(env_dir, cfg, _review_request_text(task, agent_output))
    if reply is None:
        channels = notify(
            f"[harn] '{task.id}' is ready for your review.\n"
            f"Accept:  harn review {task.id} --approve --notes \"…\"\n"
            f"Changes: harn review {task.id} --changes \"…\""
        )
        print(f"[harn] '{task.id}' awaiting review. Notified: {channels or 'none'}")
        return "pending"

    decision, payload = _parse_review_reply(reply)
    if decision == "accept":
        tasks.accept(task, notes=payload)
        progress.log(env_dir, f"{task.id}: accepted by user via Telegram")
        return "accepted"
    tasks.request_changes(task, payload)
    progress.log(env_dir, f"{task.id}: changes requested via Telegram")
    return "changes"


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
def _pick_oracle_adapter(cfg: Config) -> "Adapter":
    """Return the oracle adapter (separate agent, or same as main)."""
    name = cfg.oracle_agent or cfg.agent_chain[0]
    ad = get_adapter(name)
    return ad if ad.available() else get_adapter(cfg.agent_chain[0])


def oracle_review(
    env_dir: Path, cfg: Config, task: tasks.Task, project_root: Path,
    adapter=None,
) -> tuple[str, str, "AgentResult | None"]:
    """Independent oracle review with fresh context — used by BOTH `harn run`
    and `harn watch` (headless). Writes status to PROGRESS (so a watcher / the
    chat agent can stream it) and the verdict to the task. On FAIL it moves the
    task back to `changes_requested`.

    Returns (verdict, detail, result) where verdict is PASS / FAIL / DEBT.
    """
    adapter = adapter or _pick_oracle_adapter(cfg)
    progress.log(env_dir, f"{task.id}: oracle reviewing…", agent=adapter.name)
    diff = _git_diff(project_root)
    prompt = _build_oracle_prompt(env_dir, cfg, task, diff)
    events.emit(env_dir, "stage_start", task_id=task.id, stage="oracle",
                agent=adapter.name)
    _checkpoint_stage(project_root, task, "oracle")
    t0 = time.time()
    try:
        ores = adapter.run_turn(prompt, project_root,
                                **({"model": cfg.model} if cfg.model else {}))
    except Exception as e:  # never let the oracle crash the dispatcher
        progress.log(env_dir, f"{task.id}: oracle error: {e}", agent=adapter.name)
        events.emit(env_dir, "error", task_id=task.id, stage="oracle",
                    detail=str(e)[:300])
        return ("PASS", "", None)

    verdict, detail = _oracle_verdict(ores.text)
    events.emit(env_dir, "stage_end", task_id=task.id, stage="oracle",
                agent=adapter.name, verdict=verdict,
                tok_in=ores.input_tokens, tok_out=ores.output_tokens,
                cost_usd=ores.cost_usd, dur_ms=int((time.time() - t0) * 1000),
                summary=(detail[:200] if detail else None))
    if verdict == "FAIL":
        progress.log(env_dir,
                     f"{task.id}: oracle FAIL — {detail or '(see output)'}",
                     agent=adapter.name)
        task.review_log.append(tasks.ReviewEntry(
            ts=tasks._now_iso(), event="oracle_fail",
            agent=adapter.name, comment=detail or None))
        tasks.request_changes(task, f"[oracle] {detail or 'issues found'}",
                              by=adapter.name)
    elif verdict == "DEBT":
        progress.log(env_dir, f"{task.id}: oracle DEBT — {detail}",
                     agent=adapter.name)
        task.review_log.append(tasks.ReviewEntry(
            ts=tasks._now_iso(), event="oracle_debt",
            agent=adapter.name, comment=detail or None))
        tasks._save(task)
    else:
        progress.log(env_dir, f"{task.id}: oracle PASS", agent=adapter.name)
        task.review_log.append(tasks.ReviewEntry(
            ts=tasks._now_iso(), event="oracle_pass", agent=adapter.name))
        tasks._save(task)
    return (verdict, detail, ores)


_RECONCILE_RULES = (
    "## Rules for this reconcile pass\n"
    "- Do NOT modify any source code — this pass captures learnings ONLY.\n"
    "- For each convention/pattern/decision this task established:\n"
    "  • Confident & factual: call `save_to_skill(<skill>, \"[auto] <fact>\")` "
    "directly.\n"
    "  • Trade-off or policy needing human judgment: call "
    "`ask_user(question, skill=<skill>)` so the answer is captured into the "
    "skill. Stop after asking — resume once answered.\n"
    "- Update touched service files via `save_service` if their standards changed.\n"
    "- End your reply with `RECONCILE: DONE` (even if nothing new was found)."
)


_CHANGELOG_RECONCILE = (
    "## Changelog (release notes for this task)\n"
    "- If you have NOT yet logged the significant change(s) this task shipped, "
    "call `record_change(task_id, summary, detail)` now — ONE concise "
    "release-notes line per significant change (user-facing voice), with any "
    "decisions/standards in `detail`. Don't duplicate entries you already "
    "logged during the work; skip if already covered."
)


def _build_reconcile_prompt(env_dir: Path, cfg: Config, task: tasks.Task) -> str:
    # AGENTS.md intentionally omitted: reconcile has its own focused instructions.
    # Task spec omitted too — the brief references the task by id/title already.
    from . import skill_library
    brief = skill_library.reconcile_brief(env_dir, env_dir.parent, task)
    parts: list[str] = [_HARN_TOOLS_HINT, brief, _RECONCILE_RULES]
    if cfg.log_changes:
        parts.append(_CHANGELOG_RECONCILE)
    if not cfg.auto:
        parts.append(_autonomy_note(cfg.autonomy))
    return "\n\n".join(p for p in parts if p.strip())


def _auto_changelog(env_dir: Path, cfg: Config, task: tasks.Task,
                    summary: str = "") -> None:
    """Deterministic documentation backstop: guarantee a task leaves a changelog
    entry even if the agent never called `record_change`.

    Runs at submit. If `[log] changes` is on and the task has NO changelog entry
    yet, write one from the task's own data (title + summary + decisions) — no
    LLM, always visible. The agent's own richer entries take precedence (we only
    fire when there are none)."""
    if not cfg.log_changes or task.changelog:
        return
    detail = ""
    if task.decisions:
        detail = "Decisions: " + "; ".join(
            d.decision for d in task.decisions[:5] if d.decision)
    tasks.record_change(task, (summary or task.title).strip(), detail,
                        agent="harn-auto")
    progress.log(env_dir, f"{task.id}: changelog auto-recorded (backstop)")


def reconcile_headless(env_dir: Path, cfg: Config, task: tasks.Task,
                       project_root: Path, adapter=None) -> str:
    """Run a reconcile turn headless — used by `harn watch` so knowledge capture
    (skills/services + changelog) happens WITHOUT relying on the chat agent.

    Mirrors `oracle_review`: best-effort, never crashes the dispatcher, and
    stamps a `reconciled` review_log marker so it runs once per task. The turn
    is told NOT to ask questions (no human in this headless path) — it captures
    only confident, factual learnings."""
    adapter = adapter or _pick_adapter(cfg)
    progress.log(env_dir, f"{task.id}: auto-reconciling (headless)…",
                 agent=adapter.name)
    prompt = (_build_reconcile_prompt(env_dir, cfg, task) +
              "\n\nHEADLESS: no human is available — do NOT call ask_user. "
              "Capture only confident, factual conventions via save_to_skill / "
              "save_service, and the release note via record_change. Skip "
              "anything needing human judgement.")
    try:
        events.emit(env_dir, "stage_start", task_id=task.id, stage="reconcile",
                    agent=adapter.name)
        _checkpoint_stage(project_root, task, "reconcile")
        t0 = time.time()
        rres = adapter.run_turn(prompt, project_root,
                                **({"model": cfg.model} if cfg.model else {}))
        events.emit(env_dir, "stage_end", task_id=task.id, stage="reconcile",
                    agent=adapter.name, dur_ms=int((time.time() - t0) * 1000),
                    tok_in=rres.input_tokens, tok_out=rres.output_tokens,
                    cost_usd=rres.cost_usd)
    except Exception as e:  # never let reconcile crash the dispatcher
        progress.log(env_dir, f"{task.id}: auto-reconcile error: {e}",
                     agent=adapter.name)
        events.emit(env_dir, "error", task_id=task.id, stage="reconcile",
                    detail=str(e)[:300])
        return "error"
    # Clear any stray block the headless turn may have written (no human here).
    state.clear_block_marker(env_dir / "state")
    state.clear_block_skill(env_dir / "state")
    task = tasks.find(env_dir, task.id) or task
    _auto_changelog(env_dir, cfg, task)   # guarantee documentation
    task.review_log.append(tasks.ReviewEntry(
        ts=tasks._now_iso(), event="reconciled", agent=adapter.name))
    tasks._save(task)
    progress.log(env_dir, f"{task.id}: skills/standards reconciled", agent=adapter.name)
    return "ok"


def run_step(project_root: Path, env_dir: Path, task_id: str, step_id: str,
            *, rerun: bool = False) -> dict:
    """Run (or rerun) exactly ONE step of a task's own workflow plan, outside
    `run()`'s full multi-step cycle — the studio UI's per-step Run/Rerun
    controls and `harn run --task ID --step STEP_ID [--rerun]`.

    `rerun=True` first restores the working tree to that step's own git
    checkpoint (see `_checkpoint_stage`/gitutil.checkpoint), discarding
    whatever that step's last attempt changed, before running it again.
    Best-effort: no prior checkpoint (or no git repo) just skips the restore
    rather than failing the run.
    """
    cfg = Config.load(env_dir)
    task = tasks.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "error": f"no task '{task_id}'"}

    plan = workflows.load_task_plan(env_dir, task_id) \
        or workflows.snapshot_for_task(env_dir, task_id, task.workflow)
    step = next((n for n in plan["nodes"]
                 if n.get("kind") == "step" and n.get("id") == step_id), None)
    if step is None:
        return {"ok": False, "error": f"unknown step id {step_id!r}"}
    title = step.get("title", "")

    if step.get("type") == "command":
        plan_steps = [n for n in plan["nodes"] if n.get("kind") == "step"]
        # No explicit pre-restore here (unlike the agent-step path below):
        # `_run_command_step` unconditionally checkpoints immediately before
        # it runs the command, on every call including reruns, so the
        # checkpoint recorded for this step always reflects "right before
        # THIS attempt." A manual restore-to-the-PREVIOUS-checkpoint here
        # would additionally delete untracked files the last attempt created
        # (gitutil.rollback_to treats anything absent from that older ref's
        # tree as "created since" and removes it) — appropriate for restoring
        # git-tracked edits an agent made, but not for a raw shell command's
        # side effects, which are simply outside the checkpoint's purview.
        # A command-step "rerun" is therefore: run the command again.
        # A command step's success path never touches the agent adapter —
        # only a failed command with a live on_fail handler does. Pass None
        # here (never eagerly resolve just to run a shell command);
        # `_run_command_step` resolves a real default adapter itself, lazily,
        # only on that failure+live-handler path.
        _run_command_step(env_dir, project_root, task, step, plan_steps, cfg, None)
        fresh = tasks.find(env_dir, task_id) or task
        entry = fresh.step_results.get(step_id, {})
        return {"ok": entry.get("status") == "ok", "step_id": step_id, "title": title,
                "output": entry.get("output", "")}

    if rerun and not (step.get("parallel") or "").strip():
        # A parallel-wave member's `stage_checkpoints[step_id]` entry is the
        # WAVE's shared base ref (every member is stamped with the same one —
        # see `_run_parallel_wave`), not "right before THIS step's own last
        # attempt." Restoring to it here would wipe out every sibling's
        # already-merged edit too. For a parallel step, the caller is expected
        # to have already undone just this step's own contribution via
        # `rollback_parallel_step` (studio calls it before requesting this
        # rerun) — so skip the checkpoint-based restore entirely in that case.
        ref = task.stage_checkpoints.get(step_id)
        if ref:
            gitutil.rollback_to(ref, project_root, apply=True)

    adapter = _pick_adapter(cfg)
    step_adapter = _adapter_for_step(cfg, step, adapter)

    if not task.baseline_ref:
        tasks.set_baseline(task, gitutil.head(project_root))

    started = tasks._now_iso()
    task.step_results[step_id] = {"status": "running", "started": started,
                                  "ended": None}
    tasks._save(task)
    _checkpoint_stage(project_root, task, step_id)

    tok_totals: dict = {}
    tok_costs: dict = {}
    result = _run_turn(
        step_adapter, env_dir,
        _build_step_prompt(env_dir, cfg, task, step),
        project_root, task_id=task_id, stage=step_id, step_title=title,
        overrides=_step_overrides(cfg, step),
        tok_totals=tok_totals, tok_costs=tok_costs, cfg=cfg)

    task = tasks.find(env_dir, task_id) or task
    task.step_results[step_id] = {"status": "ok" if result.ok else "failed",
                                  "started": started, "ended": tasks._now_iso(),
                                  "tokens": result.total_tokens}
    tasks._save(task)

    return {"ok": bool(result.ok), "step_id": step_id, "title": title,
            "text": result.text}


def rollback_parallel_step(project_root: Path, env_dir: Path, task_id: str,
                           step_id: str) -> dict:
    """Undo ONE parallel-wave step's contribution to the merged main tree,
    independently of its siblings (the design spec's "Independent rollback").

    Loads the step's own saved patch (`gitutil.save_patch_ref`, written by
    `_merge_wave_patches` when the wave merged) and reverse-applies it against
    the CURRENT main tree (`gitutil.apply_patch(..., reverse=True)`, i.e.
    `git apply --3way --reverse`). If nothing has touched the same lines since
    the merge, this cleanly removes exactly that step's edits — every
    sibling's own merged patch is never touched.

    If the reverse-apply isn't possible (no patch was ever recorded for this
    step, or the patch no longer reverses cleanly because someone edited the
    same lines after the merge), falls back to the guaranteed-safe path:
    restore the WHOLE wave's shared base checkpoint via `gitutil.rollback_to`.
    `base_ref` is read from `task.stage_checkpoints[step_id]` — every member
    of a wave is stamped with that SAME shared ref when the wave runs (see
    `_run_parallel_wave`), reusing the exact per-step-checkpoint mechanism
    `run_step`'s own (non-parallel) rerun path already relies on. This
    fallback always succeeds (barring no git repo / no checkpoint at all) but
    discards every sibling's merged work too, so the result carries a `note`
    the caller (studio) MUST surface — this is a safety net, not a silent
    substitute for the single-step rollback the caller asked for.

    Returns `{"ok": True, "mode": "single-step"}` on a clean reverse-apply, or
    `{"ok": bool, "mode": "whole-wave-fallback", "note": str}` when the
    fallback fired.
    """
    task = tasks.find(env_dir, task_id)
    if task is None:
        return {"ok": False, "mode": "single-step",
                "error": f"no task '{task_id}'"}

    patch = gitutil.load_patch_ref(project_root, task.id, step_id)
    if patch and gitutil.apply_patch(project_root, patch, reverse=True):
        return {"ok": True, "mode": "single-step"}

    base_ref = task.stage_checkpoints.get(step_id, "")
    result = gitutil.rollback_to(base_ref, project_root, apply=True)
    return {
        "ok": result.ok,
        "mode": "whole-wave-fallback",
        "note": "Single-step rollback was no longer possible for this step "
                "(its saved patch no longer reverses cleanly against the "
                "current tree) — reverted the WHOLE parallel wave to its "
                "starting point instead, undoing every sibling step's edits "
                "along with this one.",
    }


def _run_end(env_dir: Path, st: "state.State") -> str:
    """Emit the terminal run_end event and return the final phase. Every exit
    from `run()` goes through here so a run's span (start→end, final phase) is
    always closed in events.jsonl."""
    events.emit(env_dir, "run_end", phase=st.phase, iterations=st.iterations)
    return st.phase


# --------------------------------------------------------------------------- #
# Parallel workflow steps (Phase 3): consecutive steps sharing a non-empty
# `parallel` id run concurrently, each isolated in its own git worktree off a
# shared checkpoint, with every connector file replicated in so ANY agent
# CLI's MCP config resolves the harn server — this is what makes a wave
# provider-agnostic (a step's `Agent:` override can be anything, per-step).
# --------------------------------------------------------------------------- #
def _collect_wave(steps: list[dict], first: dict) -> list[dict]:
    """The contiguous run of steps starting at `first` that share `first`'s
    `parallel` id. Empty/missing `parallel` → just `[first]` (not a wave).

    Only CONSECUTIVE entries count: a same-group id reappearing later, after a
    different step breaks the run, starts a NEW (separate) wave — grouping is
    purely positional, not a global id lookup.
    """
    pid = str(first.get("parallel") or "").strip()
    if not pid:
        return [first]
    idx = next((i for i, s in enumerate(steps) if s is first), None)
    if idx is None:
        return [first]
    wave = [first]
    j = idx + 1
    while j < len(steps) and str(steps[j].get("parallel") or "").strip() == pid:
        wave.append(steps[j])
        j += 1
    return wave


def _replicate_connectors(project_root: Path, worktree_path: Path,
                          env_dir: Path) -> None:
    """Copy whichever connector files exist at `project_root` into the same
    relative paths under `worktree_path`, so ANY agent CLI run there (Claude,
    Cursor, ...) discovers the harn MCP server exactly like it would in the
    main tree. For the two MCP-config shapes, the `harn` server's
    `env.HARN_ENV_DIR` is rewritten to an ABSOLUTE path — `env_dir` is the
    real (non-worktree) harn_env, and a worktree copy has no such directory of
    its own, so a relative value would resolve to nothing there.
    """
    for rel in (".mcp.json", ".cursor/mcp.json"):
        src = project_root / rel
        if not src.exists():
            continue
        dst = worktree_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            cfg_json = json.loads(src.read_text(encoding="utf-8"))
            harn_server = cfg_json.get("mcpServers", {}).get("harn")
            if harn_server is not None:
                harn_server.setdefault("env", {})["HARN_ENV_DIR"] = str(env_dir.resolve())
            dst.write_text(json.dumps(cfg_json, indent=2), encoding="utf-8")
        except (ValueError, OSError):
            shutil.copy(src, dst)  # best-effort: copy verbatim if we can't parse it
    for rel in ("AGENTS.md", "CLAUDE.md"):
        src = project_root / rel
        if src.exists():
            shutil.copy(src, worktree_path / rel)


def _conflict_markers_in_tree(project_root: Path) -> dict[str, str]:
    """Files under `project_root` (excluding `.git/`) that currently contain
    `git apply --3way` conflict markers, mapped to their full content. Used to
    build the merge-agent's prompt right after a conflicting `apply_patch`
    call — that call leaves the markers sitting in the working tree, which is
    exactly the state the merge agent needs to see and resolve."""
    found: dict[str, str] = {}
    for path in project_root.rglob("*"):
        if not path.is_file() or ".git" in path.relative_to(project_root).parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "<<<<<<<" in text and ">>>>>>>" in text:
            found[str(path.relative_to(project_root))] = text
    return found


def _run_merge_agent_turn(env_dir: Path, project_root: Path, task: "tasks.Task",
                          merged_so_far: list[dict], conflicting_step: dict,
                          cfg: Config) -> str:
    """Dispatch ONE agent turn, in the MAIN tree, to resolve a wave-merge
    conflict left by a failed `gitutil.apply_patch` call. Reuses the normal
    `_run_turn`/`_handle_block` machinery (same shape as
    `_run_onfail_handler`) rather than inventing a new agent-dispatch path.

    `merged_so_far` are the wave members whose patches already applied
    cleanly (or were themselves conflict-resolved) earlier in this same merge
    pass — named in the prompt alongside `conflicting_step` because their
    changes are what's actually sitting in the tree the conflicting patch
    collided with.

    Returns "resolved" (agent finished, whether or not it explicitly says the
    conflict is fixed — same "one attempt" contract as the on_fail handler)
    or "blocked" (the merge agent itself called `ask_user`).
    """
    markers = _conflict_markers_in_tree(project_root)
    parts = [
        "## Merge conflict in a parallel wave\n"
        "Two or more steps of this task ran CONCURRENTLY, each in its own "
        "isolated git worktree, and their changes have now been applied to "
        "this ONE shared working tree one at a time. Applying one step's "
        "changes conflicted with another's. Resolve the conflict markers "
        "below directly in this working tree so the result reflects the "
        "INTENT of every step listed, then end your turn. Do not create a "
        "git commit."
    ]
    for step in [*merged_so_far, conflicting_step]:
        parts.append(
            f"### Step: {step.get('title', '')}\n" + (step.get("body") or "").strip())
    for relpath, text in markers.items():
        parts.append(f"### Conflicting file: {relpath}\n```\n{text}\n```")
    prompt = "\n\n".join(p for p in parts if p.strip())

    adapter = _pick_adapter(cfg)
    stage = f"merge-{conflicting_step.get('id') or 'wave'}"
    _run_turn(adapter, env_dir, prompt, project_root,
             task_id=task.id, stage=stage,
             step_title=f"Merge: {conflicting_step.get('title', '')}",
             tok_totals={}, tok_costs={}, cfg=cfg)
    state_dir = env_dir / "state"
    st = state.State.load(state_dir)
    b = _handle_block(env_dir, cfg, st, state_dir, task, auto=False)
    if b == "blocked":
        return "blocked"
    # resumed / auto / no-block: the merge agent's turn is done either way —
    # stage whatever it left in the tree (clears any unmerged index entries
    # the failed `git apply --3way` left behind) and move on.
    gitutil.stage_all(project_root)
    return "resolved"


def _merge_wave_patches(env_dir: Path, project_root: Path, task: "tasks.Task",
                        wave: list[dict], results: dict, base_ref: str,
                        cfg: Config) -> str:
    """Apply each wave member's captured patch into the main tree, ONE AT A
    TIME, in wave order, and ledger the outcome.

    - Clean `gitutil.apply_patch` → ledger "ok"/"failed" (per the step's own
      `ok`), save the patch ref, move on.
    - Conflicting `apply_patch` (patch non-empty but didn't apply cleanly) →
      dispatch ONE `_run_merge_agent_turn` for just this conflict. If IT
      blocks (`ask_user`), the whole wave returns "blocked". Otherwise the
      conflict is considered resolved and the step is ledgered "ok".
    - A member that wrote `BLOCKED.md` during its OWN turn (not the merge
      turn) never gets its patch applied; its ledger entry is "blocked" and
      the wave overall returns "blocked" — but every OTHER member still
      merges normally (matching the design's "at most one live block, deferred
      to a sequential re-run" model).

    Never creates a git commit — the merged result is left as an uncommitted
    diff in `project_root`, whether the wave finishes clean or blocked.
    """
    state_dir = env_dir / "state"
    # BLOCKED.md lives in the SHARED env_dir/state/, not per-worktree, so at
    # most one wave member's block can ever be "live" here. We can't recover
    # which member wrote it from the marker alone (no attribution is
    # recorded), so we attribute it to the first member whose patch came back
    # empty — a step that stopped mid-turn to ask a question typically hasn't
    # produced tree changes yet. This is a deliberate, documented
    # simplification (see the design spec's "Blocking inside a wave" section);
    # a wave where the blocking step already made edits before asking is not
    # perfectly attributed by this heuristic.
    blocked_sid = None
    if state.read_block_question(state_dir):
        # Reuse the SAME `_handle_block` machinery a normal (non-wave) step
        # block goes through — it transitions/saves `st` (so the run's final
        # phase reports BLOCKED), waits/notifies exactly like any other block.
        st = state.State.load(state_dir)
        b = _handle_block(env_dir, cfg, st, state_dir, task, auto=False)
        if b == "blocked":
            blocked_sid = next(
                (step.get("id") or "" for step in wave
                 if results.get(step.get("id") or "") is not None
                 and not results[step.get("id") or ""][1]),
                wave[0].get("id") or "")
        # 'resumed' / 'auto': someone already answered (or auto-decided)
        # before we got here — nothing left to defer, merge normally below.

    merged_so_far: list[dict] = []
    wave_blocked = False
    for step in wave:
        sid = step.get("id") or ""
        outcome = results.get(sid)
        started = ended = tasks._now_iso()
        if outcome is None:
            # Worktree never got created — nothing to merge; record as failed
            # so it isn't silently treated as done.
            task.step_results[sid] = {"status": "failed", "started": started,
                                      "ended": ended,
                                      "output": "worktree creation failed"}
            continue
        ok, patch = outcome
        if sid == blocked_sid:
            task.step_results[sid] = {"status": "blocked", "started": started,
                                      "ended": ended}
            wave_blocked = True
            continue
        if not patch:
            task.step_results[sid] = {"status": "ok" if ok else "failed",
                                      "started": started, "ended": ended}
            continue
        applied = gitutil.apply_patch(project_root, patch)
        if applied:
            gitutil.save_patch_ref(project_root, task.id, sid, patch)
            task.step_results[sid] = {"status": "ok" if ok else "failed",
                                      "started": started, "ended": ended}
            merged_so_far.append(step)
            continue
        # Conflict: one agent-merge turn, scoped to just this step's collision
        # with whatever's already been merged into the tree.
        tasks._save(task)
        outcome_turn = _run_merge_agent_turn(env_dir, project_root, task,
                                             merged_so_far, step, cfg)
        if outcome_turn == "blocked":
            task.step_results[sid] = {"status": "blocked", "started": started,
                                      "ended": tasks._now_iso()}
            tasks._save(task)
            return "blocked"
        gitutil.save_patch_ref(project_root, task.id, sid, patch)
        task.step_results[sid] = {"status": "ok", "started": started,
                                  "ended": tasks._now_iso(),
                                  "output": "merged via agent-resolved conflict"}
        merged_so_far.append(step)
    tasks._save(task)
    progress.log(env_dir, f"{task.id}: wave {wave[0].get('parallel')} merged "
                          f"({len(wave)} step(s))")
    return "blocked" if wave_blocked else "advance"


def _run_parallel_wave(env_dir: Path, project_root: Path, task: "tasks.Task",
                       wave: list[dict], cfg: Config, default_adapter) -> str:
    """Run every step in `wave` concurrently, each in its own git worktree
    checked out from a shared checkpoint, then hand the captured patches to
    `_merge_wave_patches`.

    Returns "advance" (merged clean), "blocked" (a member's merge/handling
    escalated — treated like a normal block by the caller), or
    "conflict_unresolved" (same treatment as "blocked").
    """
    import concurrent.futures
    import tempfile

    wave_id = wave[0].get("parallel") or "wave"
    base_ref = gitutil.checkpoint(project_root, task.id, f"{wave_id}-base")
    # Stamp every member's OWN `stage_checkpoints[step_id]` with the wave's
    # shared base ref (not `_checkpoint_stage`, which would key it under
    # `f"{wave_id}-base"` instead of the step's own id). This is what lets
    # `rollback_parallel_step`'s whole-wave fallback find "the wave's starting
    # point" by looking up any one member's own step id — the SAME lookup
    # convention `run_step`'s existing rerun path already uses for ordinary
    # (non-parallel) steps, just pointed at a ref several steps share.
    if base_ref:
        for step in wave:
            sid = step.get("id") or ""
            if sid:
                task.stage_checkpoints[sid] = base_ref
        tasks._save(task)
    tmp_root = Path(tempfile.mkdtemp(prefix=f"harn-wave-{wave_id}-"))
    worktrees: dict[str, Path] = {}
    try:
        for step in wave:
            sid = step.get("id") or ""
            # Non-goal (design spec): `On fail:` combined with concurrency is
            # too combinatorial for Phase 3. Same treatment as an `on_fail`
            # pointing at a command step (Phase 2, above in `run_step`) — log
            # a config_error and otherwise run as if `on_fail` were unset. No
            # handler-dispatch code exists on this path at all, so "unset" is
            # the natural behavior; this just surfaces the mistake.
            if str(step.get("on_fail") or "").strip():
                events.emit(env_dir, "config_error", task_id=task.id, stage=sid,
                            detail="On fail is not supported inside a parallel wave (Phase 3 non-goal)")
            wt = tmp_root / (sid or wave_id)
            if gitutil.create_worktree(project_root, base_ref, wt):
                _replicate_connectors(project_root, wt, env_dir)
                worktrees[sid] = wt
            else:
                events.emit(env_dir, "config_error", task_id=task.id, stage=sid,
                            detail="failed to create worktree for parallel step")

        def _run_one(step: dict):
            sid = step.get("id") or ""
            wt = worktrees.get(sid)
            if wt is None:
                return sid, None  # worktree creation failed — nothing to run
            title = step.get("title", "")
            if step.get("type") == "command":
                events.emit(env_dir, "stage_start", task_id=task.id, stage=sid,
                            agent="command", step_title=title)
                t0 = time.time()
                res = run_feedback(step.get("command", ""), wt)
                events.emit(env_dir, "stage_end", task_id=task.id, stage=sid,
                            agent="command", step_title=title, ok=res.ok,
                            dur_ms=int((time.time() - t0) * 1000))
                ok = res.ok
            else:
                step_adapter = _adapter_for_step(cfg, step, default_adapter)
                prompt = _build_step_prompt(env_dir, cfg, task, step,
                                           parallel_note=_PARALLEL_NOTE)
                try:
                    result = _run_turn(step_adapter, env_dir, prompt, wt,
                                      task_id=task.id, stage=sid, step_title=title,
                                      overrides=_step_overrides(cfg, step),
                                      tok_totals={}, tok_costs={}, cfg=cfg)
                    ok = result.ok
                except Exception:
                    # _run_turn already emitted the `error` event before
                    # re-raising. A raising adapter must not crash the whole
                    # run() — degrade this member to a failed step instead,
                    # the same "never let a turn crash the dispatcher"
                    # convention used by oracle_review/reconcile_headless.
                    ok = False
            patch = gitutil.diff_as_patch(wt, base_ref)
            return sid, (ok, patch)

        results: dict = {}
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as ex:
                for sid, outcome in ex.map(_run_one, wave):
                    results[sid] = outcome
        finally:
            # Runs even if something above still managed to raise (defense in
            # depth) — a wave must never leak worktree admin metadata
            # (.git/worktrees/<name>/), which `shutil.rmtree(tmp_root)` alone
            # cannot clean up (that only deletes the checked-out directory,
            # not git's own bookkeeping for it).
            for sid, wt in worktrees.items():
                gitutil.remove_worktree(project_root, wt)

        return _merge_wave_patches(env_dir, project_root, task, wave, results,
                                   base_ref, cfg)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def run(project_root: Path, env_dir: Path, max_iterations: int | None = None,
        auto: bool = False, only_task: str | None = None) -> str:
    """Run the loop until DONE, BLOCKED, REVIEW (CLI), or max_iterations.

    In `auto` mode there is no human: the agent decides ambiguities itself,
    harn runs a larger iteration budget, and NO harn_env `.md` files are mutated
    (no task statuses, no PROGRESS/ANSWERS, no review log) — code may change, the
    harness bookkeeping stays pristine. Not recommended for complex tasks.

    `only_task` restricts the loop to a single task id (`harn run --task …`) —
    used by the studio UI's per-task Launch button so a background run works
    exactly the task the user picked, under the workflow they assigned it, and
    stops once it leaves the runnable pool (done/blocked/review) rather than
    picking up whatever else is next.
    """
    cfg = Config.load(env_dir)
    auto = auto or cfg.auto
    state_dir = env_dir / "state"
    st = state.State.load(state_dir)
    adapter = _pick_adapter(cfg)
    events.new_run(env_dir, kind="auto" if auto else "loop")
    if max_iterations is not None:
        limit = max_iterations
    elif auto:
        limit = cfg.auto_max_iterations
    else:
        limit = cfg.max_iterations

    if not auto and st.phase == state.BLOCKED:
        print(f"[harn] BLOCKED, waiting for an answer. Question:\n{st.question}")
        print("[harn] Provide it with: harn answer \"...\"")
        return _run_end(env_dir, st)

    mode = " [AUTO]" if auto else ""
    print(f"[harn] Agent: {adapter.name}{mode} (chain: {', '.join(cfg.agent_chain)})")
    if auto:
        print("[harn] Autonomous mode: deciding without the human; "
              "harn_env .md files are left untouched.")

    working_id: str | None = None
    feedback_tail = ""
    tok_totals: dict[str, int] = {}
    tok_costs: dict[str, float] = {}
    handled: set[str] = set()
    tests_nudged: set[str] = set()   # test-writing gate fires once per task
    last_step_text = ""              # last step's output (for the review summary)
    # In auto mode task files are never mutated, so step progress is tracked
    # in memory instead of the on-disk ledger (task.step_results).
    auto_done: dict[str, set] = {}
    # Debounces the rework reset below to fire once per rework episode, where
    # an episode boundary is "task resubmitted for review" — NOT "status left
    # CHANGES_REQUESTED". Agent-turn steps naturally leave CHANGES_REQUESTED
    # behind (the first turn calls tasks.set_status(IN_PROGRESS)), but a
    # command-only plan never does, so its status can stay CHANGES_REQUESTED
    # across multiple resubmissions within the same run() call (e.g. inline
    # review via the Telegram wait_for_reply path). Keying the reset off the
    # submit_for_review() call instead of a status transition means every
    # resubmission gets a fresh debounce, however many rework rounds happen.
    reworked: set[str] = set()
    for _ in range(limit):
        task = tasks.next_task(env_dir, exclude=handled, only=only_task)
        if task is None:
            if auto:
                st.transition(state.DONE)
                st.save(state_dir)
                print(f"[harn] Autonomous pass complete: {len(handled)} task(s) "
                      "executed (md untouched).")
                return _run_end(env_dir, st)
            in_review = tasks.tasks_in_review(env_dir)
            if in_review:
                st.transition(state.REVIEW)
                st.save(state_dir)
                ids = ", ".join(t.id for t in in_review)
                print(f"[harn] Nothing left to build; awaiting your review: {ids}")
                return _run_end(env_dir, st)
            st.transition(state.DONE)
            st.save(state_dir)
            print("[harn] No pending tasks. DONE.")
            return _run_end(env_dir, st)

        if task.id != working_id:
            working_id, feedback_tail = task.id, ""
            last_step_text = ""
            # Snapshot the task's OWN workflow plan (idempotent — from its chosen
            # preset), then render it into WORKFLOW.md so every agent
            # (Claude/Codex/Cursor) reads the exact flow this task will walk.
            workflows.snapshot_for_task(env_dir, task.id, task.workflow)
            workflows.activate_task(env_dir, task.id)
            if task.workflow:
                progress.log(env_dir, f"{task.id}: workflow → {task.workflow}",
                             agent=adapter.name)

        was_rework = task.status == tasks.CHANGES_REQUESTED
        # On rework, the prior run's steps are all ledgered "ok"; clear them so
        # the engine actually re-walks the plan to address the reviewer. Also
        # clear this task's `done_ids` (auto_done) — command steps force
        # themselves into that set on a terminal outcome, and if rework
        # resolves INLINE within the same run() call (e.g. the Telegram
        # wait_for_reply path), that set would otherwise survive the ledger
        # reset and silently skip re-walking those steps. Guarded by
        # `reworked` so this fires once per task per run() call — see note
        # on its declaration above.
        if was_rework and not auto and task.id not in reworked:
            reworked.add(task.id)
            if task.step_results:
                task.step_results = {}
                tasks._save(task)
            auto_done.pop(task.id, None)

        # The steps this task walks — its own plan's enabled step nodes, in order.
        plan = workflows.load_task_plan(env_dir, task.id) or {"nodes": []}
        steps = [n for n in plan.get("nodes", [])
                 if n.get("kind") == "step" and n.get("enabled", True) is not False]

        # A step is "done" when the on-disk ledger says ok (normal mode) or the
        # in-memory set says so (auto mode never mutates task files). `done_ids`
        # doubles as a mode-agnostic "force done" registry: command steps use
        # it to mark a step as no-longer-pending even when its ledger status
        # is "failed" (a terminal failure with no usable on_fail handler is
        # still "over" — there's nothing left that could change the outcome).
        done_ids = auto_done.setdefault(task.id, set())

        def _step_done(sid: str) -> bool:
            if sid in done_ids:
                return True
            if auto:
                return False
            return task.step_results.get(sid, {}).get("status") == "ok"

        # Find the first step not yet done (resume support).
        pending = [s for s in steps if not _step_done(s.get("id"))]

        if pending:
            step = pending[0]
            sid = step.get("id") or ""
            title = step.get("title", "")

            wave = _collect_wave(steps, step)
            if len(wave) >= 2:
                pending_wave = [s for s in wave if not _step_done(s.get("id"))]
                if pending_wave:
                    outcome = _run_parallel_wave(env_dir, project_root, task,
                                                 pending_wave, cfg, adapter)
                    if outcome in ("blocked", "conflict_unresolved"):
                        # _merge_wave_patches (inside _run_parallel_wave) loads
                        # and saves its OWN `state.State` instance when it
                        # calls `_handle_block` — reload here so `st.phase`
                        # reflects that before `_run_end` reports it.
                        st = state.State.load(state_dir)
                        return _run_end(env_dir, st)
                    for s in pending_wave:
                        done_ids.add(s.get("id") or "")
                continue

            if step.get("type") == "command":
                outcome = _run_command_step(env_dir, project_root, task, step, steps,
                                            cfg, adapter)
                if outcome == "blocked":
                    return _run_end(env_dir, st)
                if outcome == "advance":
                    # Success, or a terminal failure with no live on_fail
                    # handler — either way this step will not change again;
                    # don't let `pending` reselect it (a "failed" ledger
                    # status alone wouldn't stop that). A "handled" outcome
                    # deliberately does NOT reach here — the original command
                    # step's ledger entry stays "failed" so it IS reselected
                    # next iteration, retrying it after the handler ran.
                    done_ids.add(sid)
                continue   # "advance" or "handled" — re-evaluate `pending` next iteration

            step_adapter = _adapter_for_step(cfg, step, adapter)

            # ── STEP TURN ────────────────────────────────────────────────────
            if not auto:
                tasks.set_status(task, tasks.IN_PROGRESS)
                # Capture a git baseline the first time real work starts, so the
                # human can `harn rollback <id>` to redo the task from scratch.
                if not task.baseline_ref:
                    tasks.set_baseline(task, gitutil.head(project_root))
                # log_started only on the very first step of the task (or rework).
                first_step = not any(_step_done(s.get("id")) for s in steps)
                if first_step:
                    tasks.log_started(task, step_adapter.name, rework=was_rework)
            st.transition(state.EXECUTING)
            st.current_task = task.id
            st.iterations += 1
            st.save(state_dir)
            if not auto:
                if first_step:
                    progress.log(
                        env_dir,
                        f"{task.id}: {'reworking' if was_rework else 'started'} "
                        f"({task.title})",
                        agent=step_adapter.name,
                    )
                progress.log(
                    env_dir,
                    f"{task.id}: {title} ({step_adapter.name})",
                    agent=step_adapter.name,
                )
            print(f"[harn] {task.id} · {title} with {step_adapter.name}{mode}...")

            started = tasks._now_iso()
            if not auto:
                task.step_results[sid] = {"status": "running",
                                          "started": started, "ended": None}
                tasks._save(task)
            _checkpoint_stage(project_root, task, sid)
            result = _run_turn(
                step_adapter, env_dir,
                _build_step_prompt(env_dir, cfg, task, step, feedback_tail, auto=auto),
                project_root, task_id=task.id, stage=sid, step_title=title,
                overrides=_step_overrides(cfg, step),
                tok_totals=tok_totals, tok_costs=tok_costs, cfg=cfg)
            last_step_text = result.text or ""
            print(result.text[-2000:] if result.text else "(no output)")
            if not auto and result.usage_str():
                progress.log(env_dir, f"{task.id}: {title} used {result.usage_str()}",
                             agent=step_adapter.name)

            # 1) Did the agent block on a question? (re-runs the SAME step)
            b = _handle_block(env_dir, cfg, st, state_dir, task, auto=auto)
            if b == "resumed":
                st = state.State.load(state_dir)
                continue  # reload state, re-run the same step next iteration
            if b == "blocked":
                if not auto:
                    task.step_results[sid] = {**task.step_results.get(sid, {}),
                                              "status": "blocked"}
                    tasks._save(task)
                return _run_end(env_dir, st)
            if b == "auto":
                feedback_tail = _AUTO_DECIDE_NOTE
                continue  # let the agent decide and retry the same step

            # 2) Feedback signal (project test command) — re-run the SAME step
            fb = run_feedback(cfg.test_cmd, project_root)
            feedback_tail = fb.tail()
            if fb.ran and not fb.ok:
                print("[harn] Tests failing; looping to let the agent fix them.")
                continue  # same step, with failure as feedback

            # 2b) Test-writing gate: code changed but no tests did → one nudge
            #     (once per task; re-runs the SAME step with the nudge).
            if (cfg.require_tests and task.id not in tests_nudged
                    and _missing_tests(project_root)):
                tests_nudged.add(task.id)
                feedback_tail = _TESTS_NUDGE
                if not auto:
                    progress.log(env_dir,
                                 f"{task.id}: code changed without tests; "
                                 "asking the agent to add them",
                                 agent=step_adapter.name)
                print("[harn] Code changed without tests; looping to add them.")
                continue  # same step, with the nudge as feedback

            # Step done — ledger it and advance to the next step next iteration.
            feedback_tail = ""
            if auto:
                done_ids.add(sid)
            else:
                task.step_results[sid] = {"status": "ok", "started": started,
                                          "ended": tasks._now_iso(),
                                          "tokens": result.total_tokens}
                tasks._save(task)
                task = tasks.find(env_dir, task.id) or task
            # More steps remain? loop to run the next one.
            if any(not _step_done(s.get("id")) for s in steps):
                continue

        # ── ALL STEPS DONE ────────────────────────────────────────────────────
        result_text = last_step_text

        # 4a) AUTO: executed, but never mutate the task track — record in memory
        if auto:
            handled.add(task.id)
            events.emit(env_dir, "cycle_end", task_id=task.id,
                        outcome="auto_executed",
                        tokens=_usage_summary(tok_totals, tok_costs, task.id))
            working_id, feedback_tail = None, ""
            tok_totals.pop(task.id, None)
            tok_costs.pop(task.id, None)
            print(f"[auto] '{task.id}' executed (not marked done; md untouched).")
            continue

        # 4b) Submit for human review
        oracle_note = ""
        usage = _usage_summary(tok_totals, tok_costs, task.id)
        summary = (result_text or "").strip().splitlines()[-1:] or [""]
        # Deterministic documentation backstop: if reconcile didn't record a
        # change, guarantee one from the task's data before submitting.
        _auto_changelog(env_dir, cfg, task, summary[0][:200])
        task = tasks.find(env_dir, task.id) or task
        tasks.submit_for_review(task, adapter.name,
                                summary=summary[0][:200] + oracle_note,
                                tokens=usage)
        # Fresh rework episode starts at resubmission — see note on
        # `reworked`'s declaration above.
        reworked.discard(task.id)
        events.emit(env_dir, "cycle_end", task_id=task.id, outcome="submitted",
                    tokens=usage, summary=summary[0][:200])
        progress.log(env_dir,
                     f"{task.id}: submitted for review"
                     + (f" [{usage}]" if usage else ""),
                     agent=adapter.name)
        print(f"[harn] Task '{task.id}' submitted for review."
              + (f" Usage: {usage}" if usage else ""))

        outcome = _review_gate(env_dir, cfg, task, result_text)
        working_id, feedback_tail = None, ""
        tok_totals.pop(task.id, None)
        tok_costs.pop(task.id, None)
        if outcome == "accepted":
            st.transition(state.READY)
            st.save(state_dir)
            print(f"[harn] Task '{task.id}' accepted → done.")
        elif outcome == "changes":
            st.transition(state.READY)
            st.save(state_dir)
            print(f"[harn] Changes requested on '{task.id}'; will rework.")
        else:  # pending: leave in review, move on to other work
            st.transition(state.REVIEW)
            st.save(state_dir)

    print(f"[harn] Reached iteration limit ({limit}).")
    return _run_end(env_dir, st)


def rollback(project_root: Path, env_dir: Path, task_id: str, *, apply: bool = False,
             reopen: bool = False) -> "gitutil.RollbackResult":
    """Restore the working tree to a task's baseline (redo it from scratch).

    Dry run by default (reports what would change). With apply=True it restores
    files via git. With reopen=True it also resets the task to `todo` and clears
    its scratchpad/decisions/baseline so the next `harn run` starts it clean.
    """
    task = tasks.find(env_dir, task_id)
    if task is None:
        return gitutil.RollbackResult(False, f"no task '{task_id}'", [])
    # Never roll back harn's own bookkeeping (task JSON, PROGRESS, state).
    env_rel = env_dir.resolve().relative_to(project_root.resolve()).as_posix()
    res = gitutil.rollback_to(task.baseline_ref, project_root, apply=apply,
                              exclude=(env_rel + "/",))
    if apply and res.ok and reopen:
        task.scratchpad = ""
        task.decisions = []
        task.baseline_ref = ""
        task.stage_checkpoints = {}
        gitutil.clear_checkpoints(project_root, task_id)
        task.review_log.append(tasks.ReviewEntry(
            ts=tasks._now_iso(), event="rolled_back", by="user",
            comment=f"reverted to baseline; {res.message}",
        ))
        tasks.set_status(task, tasks.TODO)
        progress.log(env_dir, f"{task_id}: rolled back and reopened")
    return res


def answer(env_dir: Path, text: str, *, source: str = "cli") -> None:
    """Record a human answer, clear the block, and resume on next `harn run`.

    If the question was tagged with a skill (knowledge capture), the answer is
    AUTOMATICALLY promoted into that skill so harn accumulates the standard and
    never re-asks it.

    `source` — 'telegram' writes a PENDING_TELEGRAM_ANSWER.txt so the chat agent
    can pick it up via `check_pending_answer()` MCP tool when it resumes.
    """
    state_dir = env_dir / "state"
    st = state.State.load(state_dir)
    question = st.question or "(prior question)"
    skill = state.read_block_skill(state_dir)
    st.answer(text)
    state.clear_block_marker(state_dir)
    state.clear_block_skill(state_dir)
    with (state_dir / "ANSWERS.md").open("a", encoding="utf-8") as fh:
        fh.write(f"\n## Q: {question}\n{text}\n")
    st.save(state_dir)
    progress.log(env_dir, f"human answered: {text[:120]}")
    events.emit(env_dir, "answer", task_id=st.current_task, source=source,
                summary=text[:200])
    # Signal to chat agent that a Telegram/auto answer arrived while it was stopped.
    if source in ("telegram", "auto"):
        (state_dir / "PENDING_TELEGRAM_ANSWER.txt").write_text(text, encoding="utf-8")
    if skill:
        q1 = question.splitlines()[0][:160]
        skills.append_learning(env_dir, skill, f"{q1} → {text.strip()}")
        progress.log(env_dir, f"promoted answer into skill '{skill}'")


def _progress_tail_lines(env_dir: Path) -> list[str]:
    p = env_dir / "state" / "PROGRESS.md"
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def watch(env_dir: Path, project_root: Path | None = None, *, poll_s: int = 3,
          _sleep=time.sleep, _once: bool = False) -> None:
    """The dispatcher (companion to chat-mode work; not a daemon/Docker).

    A lightweight loop that runs alongside your agent (Cursor/Claude/Codex) and
    does everything that does NOT require writing code:
      • streams a LIVE status feed (new PROGRESS lines + current phase);
      • when the agent raises a question (BLOCKED.md), posts the interactive
        Telegram card with escalation, and resolves it across channels;
      • when a task hits `review`, runs the independent ORACLE headless and writes
        its verdict (the chat agent reads it and streams it to you);
      • keeps things moving without touching code.

    `_once` runs a single tick (for tests).
    """
    cfg = Config.load(env_dir)
    project_root = project_root or env_dir.parent
    state_dir = env_dir / "state"
    print(f"[harn] watch: channel={cfg.hil_channel}, grace={cfg.chat_grace_minutes}m, "
          f"oracle={'on' if cfg.oracle else 'off'}. Ctrl-C to stop.")

    handled_q: str | None = None
    oracled: set[str] = set()
    reconciled: set[str] = set()
    seen_lines = len(_progress_tail_lines(env_dir))
    last_progress_time = time.time()
    idle_notified = False

    while True:
        # 1) Live status feed — echo new PROGRESS lines as they appear.
        lines = _progress_tail_lines(env_dir)
        new_lines = lines[seen_lines:]
        for ln in new_lines:
            print(f"  │ {ln}")
        if new_lines:
            seen_lines = len(lines)
            last_progress_time = time.time()
            idle_notified = False

        # 2) Blocking question → interactive Telegram card + escalation.
        question = state.read_block_question(state_dir)
        if question and question != handled_q:
            print(f"[harn] watch: question raised → routing to {cfg.hil_channel}")
            st = state.State.load(state_dir)
            st.block(question)
            st.save(state_dir)
            reply, reply_src = _await_answer(env_dir, cfg, st.current_task or "(chat)",
                                             question)
            if reply_src == "telegram" and reply is not None:
                answer(env_dir, reply, source="telegram")
                print("[harn] watch: answered via Telegram.")
            elif reply_src == "chat":
                print("[harn] watch: answered in chat.")
            elif reply_src == "auto":
                answer(env_dir, "You pressed 'Decide for me'. " + _AUTO_BUTTON_NOTE,
                       source="auto")
                print("[harn] watch: delegated to the agent.")
            else:
                notify(f"[harn] Agent needs your input:\n{question}")
            handled_q = question
            last_progress_time = time.time()
            idle_notified = False
        elif not question:
            handled_q = None

        # 2b) Idle detection — Telegram nudge when agent is silently working.
        # Only fires once per idle period; resets when new progress appears.
        if (not question and not idle_notified and cfg.wait_for_reply):
            idle_s = time.time() - last_progress_time
            if idle_s >= cfg.chat_grace_minutes * 60:
                tg = TelegramHIL.from_env()
                if tg:
                    st_idle = state.State.load(state_dir)
                    current = st_idle.current_task
                    phase = st_idle.phase
                    if current and phase not in (state.REVIEW, state.DONE,
                                                 state.BLOCKED):
                        mins = int(idle_s / 60)
                        tg.send(
                            f"⏳ Agent working on {current} — "
                            f"no updates for {mins} min.\n"
                            "Still running; you'll hear when it's done or needs you."
                        )
                        idle_notified = True

        # 3) Tasks in review → run oracle + reconcile headless (knowledge capture
        #    that does NOT depend on the chat agent remembering to).
        if cfg.oracle:
            for t in tasks.tasks_in_review(env_dir):
                if t.id in oracled:
                    continue
                if any(e.event.startswith("oracle_") for e in t.review_log):
                    oracled.add(t.id)
                    continue
                print(f"[harn] watch: running oracle on '{t.id}' (headless)…")
                verdict, detail, _ = oracle_review(env_dir, cfg, t, project_root)
                print(f"[harn] watch: oracle {verdict}"
                      + (f" — {detail}" if detail else ""))
                oracled.add(t.id)

        # 3b) Auto-reconcile: enrich skills/services + changelog for tasks in
        #     review that haven't been reconciled yet. This is what makes the
        #     knowledge base grow after EVERY task without the agent's discipline.
        if cfg.auto_reconcile:
            for t in tasks.tasks_in_review(env_dir):
                if t.id in reconciled:
                    continue
                if any(e.event == "reconciled" for e in t.review_log):
                    reconciled.add(t.id)
                    continue
                print(f"[harn] watch: auto-reconciling '{t.id}' (headless)…")
                reconcile_headless(env_dir, cfg, t, project_root)
                reconciled.add(t.id)

        if _once:
            return
        _sleep(poll_s)


def review(env_dir: Path, task_id: str, *, approve: bool, notes: str = "",
           changes: str = "") -> str:
    """CLI review decision: accept (with notes) or request changes."""
    task = tasks.find(env_dir, task_id)
    if task is None:
        raise ValueError(f"no task '{task_id}'")
    if not task.in_review:
        raise ValueError(f"task '{task_id}' is '{task.status}', not in review")
    if approve:
        tasks.accept(task, notes=notes)
        progress.log(env_dir, f"{task_id}: accepted by user (CLI)")
        return "accepted"
    tasks.request_changes(task, changes)
    progress.log(env_dir, f"{task_id}: changes requested by user (CLI)")
    return "changes"
