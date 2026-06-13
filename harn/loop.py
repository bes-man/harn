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

import re
import subprocess
import time
from pathlib import Path

from . import browser, design as design_mod, gitutil, progress, prd as prd_mod, \
    semble_bridge, skills, state, tasks
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


def _autonomy_note(level: float) -> str:
    """Translate the 0.0–1.0 autonomy level into a behavioural directive."""
    pct = int(round(level * 100))
    if level <= 0.3:
        stance = (
            "Be METICULOUS. Surface every ambiguity, missing detail, or "
            "assumption and call `ask_user` BEFORE acting. Prefer asking over "
            "deciding — the human wants tight control over direction."
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

# Max consecutive planning turns per task before harn proceeds to execution
# even without an explicit lock_spec — a backstop against a non-locking agent
# burning the iteration budget on the funnel.
_MAX_PLAN_TURNS = 3


def _planning_instructions(cfg: Config) -> str:
    hint = semble_bridge.planning_hint(cfg)
    search_step = (
        "\n2. **Search existing patterns**: " + hint.splitlines()[-1]
        if hint else ""
    )
    n = 3 if hint else 2
    return f"""\
## PLANNING PHASE — a funnel, not a code session (write no code yet)

Goal: NARROW the task from many possible interpretations down to ONE verified,
unambiguous spec. Each question should eliminate the most uncertainty; after
each answer, DROP the options it ruled out. Converge, then lock.

{hint}

1. **Read** the task and every referenced PRD carefully.{search_step}
{n}. **Funnel the ambiguity**: surface the HIGHEST-LEVERAGE open question first \
(the one whose answer collapses the most branches), via `ask_user` (expanded: \
context + 2-3 options + your recommendation). One at a time — never dump a \
questionnaire. After each answer, narrow scope and discard the rejected options. \
Repeat only while real ambiguity remains. Never guess on scope.
{n+1}. **Lock the spec**: when nothing material is left open, call `lock_spec` \
with the final `done_when` (concrete, independently verifiable criteria — one \
observable fact per line), the chosen `approach` (rejected alternatives dropped), \
and the `decisions` you settled with the human. This is the funnel's output: a \
tight spec the executor implements verbatim, so the full PRD need not be re-read.
{n+2}. **Skills**: confirm the task's `skills` covers what the executor needs; \
`update_task` if not.

Do NOT touch the codebase. End the turn by calling `lock_spec`. Planning is \
complete once the spec is locked.
"""


def _design_instructions(cfg: Config, task: tasks.Task) -> str:
    """Planning-phase step for user-facing tasks: mockup before code."""
    if not cfg.design:
        return ""
    return f"""\
## DESIGN — only if this task has a user-facing surface

If the task changes or creates anything the user will SEE (a page, screen,
component, dashboard, form), produce the interface design BEFORE implementation:

1. Generate a single-file static HTML mockup of the final interface — inline
   CSS, realistic sample data, every state the acceptance criteria mention —
   and save it with the `save_design` MCP tool (it lands in
   `harn_env/design/{task.id}.html`).
2. Confirm it with the human: call `ask_user` summarising the mockup (layout,
   key elements, flows) and pointing at the file. Iterate until approved.
3. Ensure the task's `skills` list includes "ui" (via `update_task`) so the
   browser-verification phase runs on this task.

Once approved, the mockup is the visual contract: the executor builds to it and
the verification phases check the real UI against it. If the task has no
user-facing surface, skip this section entirely.
"""


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


def _build_planning_prompt(env_dir: Path, cfg: Config, task: tasks.Task) -> str:
    agents_md = env_dir.parent / "AGENTS.md"
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    task_body = _task_spec(task)

    parts: list[str] = [base]
    if task.prds:
        prd_parts = []
        for pid in task.prds:
            p = prd_mod.find(env_dir, pid)
            prd_parts.append(
                f"### PRD: {pid}\n\n{p.raw.strip()}" if p
                else f"### PRD: {pid}\n(not found — create `harn_env/prd/{pid}.md`)"
            )
        parts.append("## Referenced PRDs\n" + "\n\n---\n\n".join(prd_parts))
    parts.append(f"## Task to clarify — {task.id}\n" + task_body)
    parts.append(_autonomy_note(cfg.autonomy))
    parts.append(_planning_instructions(cfg))
    parts.append(_design_instructions(cfg, task))
    return "\n\n".join(p for p in parts if p.strip())


def _build_oracle_prompt(
    env_dir: Path, cfg: Config, task: tasks.Task, diff: str
) -> str:
    agents_md = env_dir.parent / "AGENTS.md"
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    task_body = _task_spec(task)

    # Oracle instructions are diff-aware: include blast-radius guidance scoped
    # to exactly the files that changed (SocratiCode first, semble fallback).
    parts: list[str] = [base, _oracle_instructions(diff, cfg)]
    if task.prds:
        prd_parts = []
        for pid in task.prds:
            p = prd_mod.find(env_dir, pid)
            if p:
                prd_parts.append(f"### PRD: {pid}\n\n{p.raw.strip()}")
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


_LIFECYCLE_NOTE = (
    "## How harn runs (you are one turn of a loop)\n"
    "harn picks the top task and you do ONE focused turn. Then harn runs the "
    "project's tests, VERIFIES your work against the task's acceptance criteria, "
    "and submits it for HUMAN review. The reviewer either accepts it (→ `done`) "
    "or replies with changes — in which case the task comes back to you as "
    "`changes_requested` with their comment in its Review log. Use the harn MCP "
    "tools (`get_next_task`, `read_skill`, `run_tests`, `board`, "
    "`submit_for_review`). If anything is ambiguous or risky, call `ask_user` "
    "(or write `harn_env/state/BLOCKED.md`) and STOP — do not guess.\n"
    "BEFORE any code, run the pre-task protocol from AGENTS.md (AS IS → TO BE → "
    "skills, named → best practices → clarify). BEFORE you submit, RECONCILE: "
    "`save_to_skill` for confident conventions ([auto]), `ask_user(skill=…)` "
    "for trade-offs, refresh touched service files. harn learns from its work.\n"
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


def _build_prompt(
    env_dir: Path, cfg: Config, task: tasks.Task, feedback_tail: str = "",
    auto: bool = False,
) -> str:
    state_dir = env_dir / "state"
    agents_md = env_dir.parent / "AGENTS.md"
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    task_body = _task_spec(task)

    # Prompt is ordered STABLE-FIRST, VOLATILE-LAST so the agent CLI's automatic
    # prompt caching (Anthropic 5-min prefix cache) reuses the longest possible
    # prefix across turns. Keep per-turn-changing content (board, progress,
    # answers, feedback) at the END — see the volatile tail below. Do NOT move
    # volatile blocks up here or every turn busts the cache for everything after.
    parts: list[str] = [base]

    if auto:
        parts.append(_AUTO_NOTE)

    if cfg.loop_aware:
        parts.append(_LIFECYCLE_NOTE)

    parts.append(
        "## Available skills (load only what you need)\n"
        "Read a skill's full text via the harn `read_skill` tool ONLY when the "
        "task calls for it, to keep the context window small:\n" + skills.index(env_dir)
    )
    # Flag domains the task needs but no skill covers; tell the agent to bootstrap.
    from . import skill_library
    gap_note = skill_library.gap_note(env_dir, task)
    if gap_note:
        parts.append(gap_note)
    # Persistent AS-IS knowledge: the service registry index (or the
    # instruction to seed it) — answers the AS-IS step without re-indexing.
    from . import codebase as codebase_mod
    parts.append(codebase_mod.prompt_note(env_dir))
    # Inject PRD context. Once the funnel has locked the spec, the task already
    # carries the distilled criteria — inject only a COMPACT reference and let
    # the agent `read_prd` on demand (big recurring token saving, no fidelity
    # loss). Before lock (planning), inject the full PRD so questions converge.
    if task.prds:
        if task.spec_locked:
            refs: list[str] = []
            for prd_id in task.prds:
                p = prd_mod.find(env_dir, prd_id)
                if p:
                    why = (p.sections.get("Problem")
                           or p.sections.get("Goal") or "").strip().splitlines()
                    one = (why[0] if why else "")[:160]
                    refs.append(f"- **{p.id}** — {p.title}: {one} "
                                f"(`read_prd(\"{p.id}\")` for full text)")
                else:
                    refs.append(f"- **{prd_id}** (missing — create "
                                f"`harn_env/prd/{prd_id}.md`)")
            parts.append(
                "## Task lineage (spec locked — PRD on demand)\nThe `## Done "
                "when` below is the verified, narrowed spec. Implement exactly "
                "that; read a PRD only if you need the deeper why:\n"
                + "\n".join(refs))
        else:
            prd_parts: list[str] = []
            for prd_id in task.prds:
                p = prd_mod.find(env_dir, prd_id)
                if p:
                    prd_parts.append(f"### PRD: {p.id} — {p.title}\n\n{p.raw.strip()}")
                else:
                    prd_parts.append(
                        f"### PRD: {prd_id}\n(file not found — create "
                        f"`harn_env/prd/{prd_id}.md`)")
            prd_header = (
                f"This task belongs to PRD(s): **{', '.join(task.prds)}**. "
                "Read them for the why, scope, and acceptance criteria.")
            if task.epic:
                prd_header += f" Epic: `{task.epic}`."
            if task.user_story:
                prd_header += f" User story: `{task.user_story}`."
            hint = prd_mod.normalisation_hint(
                [p for pid in task.prds
                 for p in ([prd_mod.find(env_dir, pid)] if prd_mod.find(env_dir, pid) else [])]
            )
            parts.append(
                "## Task lineage\n" + prd_header
                + ("\n\n" + hint if hint else "")
                + "\n\n" + "\n\n---\n\n".join(prd_parts))
    parts.append(f"## Current task — {task.id} (status: {task.status})\n" + task_body)
    dz = _design_block(env_dir, task.id)
    if dz:
        parts.append(dz)
    cont = _continuity_block(task)
    if cont:
        parts.append(cont)
    if not auto:
        parts.append(_autonomy_note(cfg.autonomy))
    parts.append(
        "## Rules\n"
        "- When unsure, follow the autonomy level above: ask via the harn "
        "`ask_user` tool (or write `harn_env/state/BLOCKED.md` and end your turn) "
        "rather than guessing on anything you shouldn't decide alone.\n"
        "- " + _ASK_GUIDANCE + "\n"
        "- If this task is `changes_requested`, read its Review log and address "
        "the reviewer's comment.\n"
        "- When the task is complete and tests pass, say what you did so the "
        "human can review it."
    )
    # --- Volatile tail (changes every turn → kept last for cache hits) -------
    if cfg.loop_aware:
        parts.append("## Task board\n" + tasks.board(env_dir))
        prog = progress.tail(env_dir)
        if prog:
            parts.append("## Progress so far (shared across agents)\n" + prog)
        answers = _answers_tail(state_dir)
        if answers:
            parts.append("## Earlier answers from the human\n" + answers)
    if feedback_tail:
        parts.append("## Last feedback (tests)\n```\n" + feedback_tail + "\n```")
    return "\n\n".join(p for p in parts if p.strip())


_VERIFY_INSTRUCTIONS = (
    "## You are VERIFYING — do not start new work\n"
    "This is a verification pass for the task above, before it goes to a human.\n"
    "1. Re-read the task's acceptance criteria (its 'Done when' / scope).\n"
    "2. Check the ACTUAL implementation satisfies EACH criterion — not just that "
    "tests pass. Use `run_tests` and read the relevant code.\n"
    "3. If something is missing, wrong, or low-quality and you can safely fix it "
    "now, fix it.\n"
    "4. If resolving it needs a human decision (ambiguous, risky, a product "
    "choice), call `ask_user` with an EXPANDED question (context + options + your "
    "recommendation) and STOP.\n"
    "5. End your reply with exactly one line: `VERIFY: PASS` (criteria met) or "
    "`VERIFY: FAIL` (gaps remain)."
)


def _build_verify_prompt(
    env_dir: Path, cfg: Config, task: tasks.Task, auto: bool = False
) -> str:
    agents_md = env_dir.parent / "AGENTS.md"
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    task_body = _task_spec(task)
    parts = [
        base,
        f"## Task under verification — {task.id}\n" + task_body,
        _continuity_block(task),
        _VERIFY_INSTRUCTIONS,
        _AUTO_NOTE if auto else _ASK_GUIDANCE,
    ]
    return "\n\n".join(p for p in parts if p.strip())


def _verify_verdict(text: str) -> str:
    """'fail' only on an explicit VERIFY: FAIL; else 'pass' (lenient)."""
    t = (text or "").lower()
    if "verify: fail" in t and "verify: pass" not in t:
        return "fail"
    return "pass"


# --------------------------------------------------------------------------- #
# Browser verification (Playwright MCP) — check the criteria in the LIVE app
# --------------------------------------------------------------------------- #
def _ui_applicable(env_dir: Path, task: tasks.Task) -> bool:
    """A task gets the browser pass when it has an approved design or is
    explicitly tagged with the 'ui' skill (the planner sets one of the two)."""
    return design_mod.exists(env_dir, task.id) or "ui" in task.skills


def _build_ui_verify_prompt(
    env_dir: Path, cfg: Config, task: tasks.Task, shots_dir: Path,
    auto: bool = False,
) -> str:
    agents_md = env_dir.parent / "AGENTS.md"
    base = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
    instructions = f"""\
## You are VERIFYING THE LIVE UI — do not start new work

The app is RUNNING at **{cfg.app_url}**. Use the Playwright MCP tools
(`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`,
`browser_take_screenshot`, …) to check the task's acceptance criteria against
the real interface, the way a user would:

1. Walk EVERY user-visible acceptance criterion in the task's `## Done when` —
   navigate, click, fill forms, observe the result. Not just the happy path:
   try empty input, wrong input, repeated submits where the criteria imply them.
2. Take a screenshot of each verified state and save it under `{shots_dir}`
   (one file per criterion, named after what it shows).
3. If there is an approved design above, compare what you see against it —
   layout, key elements, states must match.
4. If something is broken or diverges and you can safely fix it in code, fix it
   (the loop will re-run tests). If it needs a human decision, call `ask_user`
   (expanded) and STOP.
5. End your reply with exactly one line: `UI: PASS` or `UI: FAIL — <reason>`.
"""
    parts = [
        base,
        f"## Task under UI verification — {task.id}\n" + _task_spec(task),
        _design_block(env_dir, task.id),
        _continuity_block(task),
        instructions,
        _AUTO_NOTE if auto else _ASK_GUIDANCE,
    ]
    return "\n\n".join(p for p in parts if p.strip())


def _ui_verdict(text: str) -> str:
    """'fail' only on an explicit UI: FAIL; else 'pass' (lenient)."""
    t = (text or "").lower()
    if "ui: fail" in t and "ui: pass" not in t:
        return "fail"
    return "pass"


# --------------------------------------------------------------------------- #
# Token accounting (best-effort: only agents that report usage, e.g. Claude)
# --------------------------------------------------------------------------- #
def _accumulate(totals: dict, costs: dict, task_id: str, r) -> None:
    if r.total_tokens is not None:
        totals[task_id] = totals.get(task_id, 0) + r.total_tokens
    if r.cost_usd is not None:
        costs[task_id] = costs.get(task_id, 0.0) + r.cost_usd


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
def _run_verify(adapter, env_dir, cfg, task, project_root, st, state_dir, auto,
                tok_totals, tok_costs) -> str:
    """Run the verification turn. Returns 'ok', 'resumed', 'blocked', or 'loop'
    (caller should rework the same task)."""
    st.transition(state.VERIFYING)
    st.save(state_dir)
    if not auto:
        progress.log(env_dir, f"{task.id}: verifying against acceptance criteria",
                     agent=adapter.name)
    print(f"[harn] Verifying '{task.id}'…")
    vres = adapter.run_turn(_build_verify_prompt(env_dir, cfg, task, auto=auto),
                            project_root)
    print(vres.text[-1500:] if vres.text else "(verify: no output)")
    _accumulate(tok_totals, tok_costs, task.id, vres)
    if not auto and vres.usage_str():
        progress.log(env_dir, f"{task.id}: verify used {vres.usage_str()}",
                     agent=adapter.name)

    b = _handle_block(env_dir, cfg, st, state_dir, task, auto=auto)
    if b == "resumed":
        return "resumed"
    if b == "blocked":
        return "blocked"
    if b == "auto":
        return "loop"

    fb = run_feedback(cfg.test_cmd, project_root)
    if fb.ran and not fb.ok:
        print("[harn] Verify left tests failing; looping to fix.")
        return "loop"
    if _verify_verdict(vres.text) == "fail":
        if not auto:
            progress.log(env_dir, f"{task.id}: verify found gaps; reworking",
                         agent=adapter.name)
        print("[harn] Verify reported gaps; looping to address them.")
        return "loop"
    return "ok"


def _run_ui_verify(adapter, env_dir, cfg, task, project_root, st, state_dir,
                   auto, tok_totals, tok_costs) -> str:
    """Run the browser-verification turn against the live app. Returns 'ok',
    'resumed', 'blocked', 'loop' (rework), or 'skipped' (app not reachable —
    surfaced to PROGRESS, never blocks the pipeline)."""
    app = browser.start_app(cfg.app_cmd, cfg.app_url, project_root,
                            cfg.ready_timeout_s)
    if not app.ready:
        progress.log(env_dir,
                     f"{task.id}: browser verify SKIPPED — {app.error}",
                     agent=adapter.name)
        print(f"[harn] Browser verify skipped: {app.error}")
        return "skipped"

    st.transition(state.UI_VERIFYING)
    st.save(state_dir)
    shots_dir = env_dir / "state" / "screenshots" / task.id
    shots_dir.mkdir(parents=True, exist_ok=True)
    if not auto:
        progress.log(env_dir, f"{task.id}: verifying the live UI via Playwright",
                     agent=adapter.name)
    print(f"[harn] Browser-verifying '{task.id}' at {cfg.app_url}…")
    try:
        ures = adapter.run_turn(
            _build_ui_verify_prompt(env_dir, cfg, task, shots_dir, auto=auto),
            project_root)
    finally:
        app.stop()
    print(ures.text[-1500:] if ures.text else "(ui verify: no output)")
    _accumulate(tok_totals, tok_costs, task.id, ures)
    if not auto and ures.usage_str():
        progress.log(env_dir, f"{task.id}: ui verify used {ures.usage_str()}",
                     agent=adapter.name)

    b = _handle_block(env_dir, cfg, st, state_dir, task, auto=auto)
    if b in ("resumed", "blocked"):
        return b
    if b == "auto":
        return "loop"

    fb = run_feedback(cfg.test_cmd, project_root)
    if fb.ran and not fb.ok:
        print("[harn] UI verify left tests failing; looping to fix.")
        return "loop"
    if _ui_verdict(ures.text) == "fail":
        if not auto:
            progress.log(env_dir, f"{task.id}: UI verify found gaps; reworking",
                         agent=adapter.name)
        print("[harn] UI verify reported gaps; looping to address them.")
        return "loop"
    if not auto:
        progress.log(env_dir, f"{task.id}: UI verify PASS", agent=adapter.name)
    return "ok"


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
    try:
        ores = adapter.run_turn(prompt, project_root)
    except Exception as e:  # never let the oracle crash the dispatcher
        progress.log(env_dir, f"{task.id}: oracle error: {e}", agent=adapter.name)
        return ("PASS", "", None)

    verdict, detail = _oracle_verdict(ores.text)
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


def _run_oracle(
    oracle_adapter, env_dir: Path, cfg: Config, task: tasks.Task,
    project_root: Path, st: "state.State", state_dir: Path,
    tok_totals: dict, tok_costs: dict,
) -> str:
    """In-loop oracle wrapper for `harn run`. Returns 'ok' | 'loop' | 'debt'."""
    print(f"[harn] Oracle reviewing '{task.id}' ({oracle_adapter.name})…")
    verdict, detail, ores = oracle_review(env_dir, cfg, task, project_root,
                                          adapter=oracle_adapter)
    if ores is not None:
        _accumulate(tok_totals, tok_costs, task.id, ores)
    if verdict == "FAIL":
        return "loop"   # oracle_review set changes_requested; loop reworks it
    return "debt" if verdict == "DEBT" else "ok"


def run(project_root: Path, env_dir: Path, max_iterations: int | None = None,
        auto: bool = False) -> str:
    """Run the loop until DONE, BLOCKED, REVIEW (CLI), or max_iterations.

    In `auto` mode there is no human: the agent decides ambiguities itself,
    harn runs a larger iteration budget, and NO harn_env `.md` files are mutated
    (no task statuses, no PROGRESS/ANSWERS, no review log) — code may change, the
    harness bookkeeping stays pristine. Not recommended for complex tasks.
    """
    cfg = Config.load(env_dir)
    auto = auto or cfg.auto
    state_dir = env_dir / "state"
    st = state.State.load(state_dir)
    adapter = _pick_adapter(cfg)
    if max_iterations is not None:
        limit = max_iterations
    elif auto:
        limit = cfg.auto_max_iterations
    else:
        limit = cfg.max_iterations

    if not auto and st.phase == state.BLOCKED:
        print(f"[harn] BLOCKED, waiting for an answer. Question:\n{st.question}")
        print("[harn] Provide it with: harn answer \"...\"")
        return st.phase

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
    plan_turns: dict[str, int] = {}  # funnel turns per task (capped)
    for _ in range(limit):
        task = tasks.next_task(env_dir, exclude=handled)
        if task is None:
            if auto:
                st.transition(state.DONE)
                st.save(state_dir)
                print(f"[harn] Autonomous pass complete: {len(handled)} task(s) "
                      "executed (md untouched).")
                return st.phase
            in_review = tasks.tasks_in_review(env_dir)
            if in_review:
                st.transition(state.REVIEW)
                st.save(state_dir)
                ids = ", ".join(t.id for t in in_review)
                print(f"[harn] Nothing left to build; awaiting your review: {ids}")
                return st.phase
            st.transition(state.DONE)
            st.save(state_dir)
            print("[harn] No pending tasks. DONE.")
            return st.phase

        if task.id != working_id:
            working_id, feedback_tail = task.id, ""

        was_rework = task.status == tasks.CHANGES_REQUESTED
        is_new = not task.review_log      # never been touched → planning candidate

        # ── PLANNING TURN(S) ─────────────────────────────────────────────────
        # The clarification funnel: keep planning until the agent locks the spec
        # (`lock_spec` → task.spec_locked). Multiple narrowing questions can span
        # several turns. Skipped in auto mode and on rework (spec already locked).
        # Capped at _MAX_PLAN_TURNS so a non-locking agent can't burn the budget;
        # past the cap we proceed to execution with the current description.
        if (cfg.planning and not task.spec_locked and not auto and not was_rework
                and plan_turns.get(task.id, 0) < _MAX_PLAN_TURNS):
            plan_turns[task.id] = plan_turns.get(task.id, 0) + 1
            st.transition(state.PLANNING)
            tasks.set_status(task, tasks.IN_PROGRESS)
            if is_new:
                task.review_log.append(tasks.ReviewEntry(
                    ts=tasks._now_iso(), event="planning_started", agent=adapter.name,
                ))
            tasks._save(task)
            st.current_task = task.id
            st.iterations += 1
            st.save(state_dir)
            progress.log(env_dir, f"{task.id}: planning — clarifying requirements",
                         agent=adapter.name)
            print(f"[harn] Planning '{task.id}' ({task.title}) with {adapter.name}…")

            pres = adapter.run_turn(_build_planning_prompt(env_dir, cfg, task),
                                    project_root)
            print(pres.text[-2000:] if pres.text else "(planning: no output)")
            _accumulate(tok_totals, tok_costs, task.id, pres)
            if pres.usage_str():
                progress.log(env_dir, f"{task.id}: planning used {pres.usage_str()}",
                             agent=adapter.name)

            b = _handle_block(env_dir, cfg, st, state_dir, task, auto=False)
            if b == "resumed":
                st = state.State.load(state_dir)
                continue
            if b == "blocked":
                return st.phase
            # Reload task — the agent may have called lock_spec this turn.
            task = tasks.find(env_dir, task.id) or task
            if task.spec_locked:
                progress.log(env_dir, f"{task.id}: spec locked — planning complete",
                             agent=adapter.name)
            else:
                progress.log(env_dir, f"{task.id}: planning continues (spec not "
                             "locked yet)", agent=adapter.name)
            continue   # loop: execution if locked, else another planning turn

        # ── EXECUTION TURN ────────────────────────────────────────────────────
        if not auto:
            tasks.set_status(task, tasks.IN_PROGRESS)
            # Capture a git baseline the first time real work starts, so the
            # human can `harn rollback <id>` to redo the task from scratch.
            if not task.baseline_ref:
                tasks.set_baseline(task, gitutil.head(project_root))
            tasks.log_started(task, adapter.name, rework=was_rework)
        st.transition(state.EXECUTING)
        st.current_task = task.id
        st.iterations += 1
        st.save(state_dir)
        if not auto:
            progress.log(
                env_dir,
                f"{task.id}: {'reworking' if was_rework else 'started'} ({task.title})",
                agent=adapter.name,
            )
        print(f"[harn] Working task '{task.id}' ({task.title}) with {adapter.name}{mode}...")

        result = adapter.run_turn(
            _build_prompt(env_dir, cfg, task, feedback_tail, auto=auto), project_root)
        print(result.text[-2000:] if result.text else "(no output)")
        _accumulate(tok_totals, tok_costs, task.id, result)
        if not auto and result.usage_str():
            progress.log(env_dir, f"{task.id}: turn used {result.usage_str()}",
                         agent=adapter.name)

        # 1) Did the agent block on a question?
        b = _handle_block(env_dir, cfg, st, state_dir, task, auto=auto)
        if b == "resumed":
            st = state.State.load(state_dir)
            continue  # task stays in_progress → resumed next iteration
        if b == "blocked":
            return st.phase
        if b == "auto":
            feedback_tail = _AUTO_DECIDE_NOTE
            continue  # let the agent decide and retry the same task

        # 2) Feedback signal (project test command)
        fb = run_feedback(cfg.test_cmd, project_root)
        feedback_tail = fb.tail()
        if fb.ran and not fb.ok:
            print("[harn] Tests failing; looping to let the agent fix them.")
            continue  # task stays in_progress → another turn

        # 2b) Test-writing gate: code changed but no tests did → one nudge
        if (cfg.require_tests and task.id not in tests_nudged
                and _missing_tests(project_root)):
            tests_nudged.add(task.id)
            feedback_tail = _TESTS_NUDGE
            if not auto:
                progress.log(env_dir,
                             f"{task.id}: code changed without tests; "
                             "asking the agent to add them",
                             agent=adapter.name)
            print("[harn] Code changed without tests; looping to add them.")
            continue  # same task, with the nudge as feedback

        # 3) Verify the work against the task's acceptance criteria
        if cfg.verify:
            v = _run_verify(adapter, env_dir, cfg, task, project_root, st,
                            state_dir, auto, tok_totals, tok_costs)
            if v == "resumed":
                st = state.State.load(state_dir)
                continue
            if v == "blocked":
                return st.phase
            if v == "loop":
                continue  # rework the same task

        # 3a) Browser verification: drive the LIVE app through Playwright MCP
        if cfg.browser_enabled and _ui_applicable(env_dir, task):
            u = _run_ui_verify(adapter, env_dir, cfg, task, project_root, st,
                               state_dir, auto, tok_totals, tok_costs)
            if u == "resumed":
                st = state.State.load(state_dir)
                continue
            if u == "blocked":
                return st.phase
            if u == "loop":
                continue  # rework the same task

        # 3b) Oracle: independent agent checks correctness + technical debt
        oracle_note = ""
        if cfg.oracle and not auto:
            oracle_adapter = _pick_oracle_adapter(cfg)
            ov = _run_oracle(oracle_adapter, env_dir, cfg, task, project_root,
                             st, state_dir, tok_totals, tok_costs)
            if ov == "loop":
                continue   # oracle found real failure → rework
            if ov == "debt":
                debt_entry = next(
                    (e for e in reversed(task.review_log) if e.event == "oracle_debt"),
                    None,
                )
                oracle_note = (
                    f"\n\n⚠️ **Oracle flagged technical debt**: "
                    f"{debt_entry.comment if debt_entry else '(see log)'}"
                )
            task = tasks.find(env_dir, task.id) or task   # reload after oracle writes

        # 4a) AUTO: executed, but never mutate the task track — record in memory
        if auto:
            handled.add(task.id)
            working_id, feedback_tail = None, ""
            tok_totals.pop(task.id, None)
            tok_costs.pop(task.id, None)
            print(f"[auto] '{task.id}' executed (not marked done; md untouched).")
            continue

        # 4b) Submit for human review
        usage = _usage_summary(tok_totals, tok_costs, task.id)
        summary = (result.text or "").strip().splitlines()[-1:] or [""]
        tasks.submit_for_review(task, adapter.name,
                                summary=summary[0][:200] + oracle_note,
                                tokens=usage)
        progress.log(env_dir,
                     f"{task.id}: submitted for review"
                     + (f" [{usage}]" if usage else ""),
                     agent=adapter.name)
        print(f"[harn] Task '{task.id}' submitted for review."
              + (f" Usage: {usage}" if usage else ""))

        outcome = _review_gate(env_dir, cfg, task, result.text)
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
    return st.phase


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
        task.review_log.append(tasks.ReviewEntry(
            ts=tasks._now_iso(), event="rolled_back", by="user",
            comment=f"reverted to baseline; {res.message}",
        ))
        tasks.set_status(task, tasks.TODO)
        progress.log(env_dir, f"{task_id}: rolled back and reopened")
    return res


def answer(env_dir: Path, text: str) -> None:
    """Record a human answer, clear the block, and resume on next `harn run`.

    If the question was tagged with a skill (knowledge capture), the answer is
    AUTOMATICALLY promoted into that skill so harn accumulates the standard and
    never re-asks it.
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
    seen_lines = len(_progress_tail_lines(env_dir))

    while True:
        # 1) Live status feed — echo new PROGRESS lines as they appear.
        lines = _progress_tail_lines(env_dir)
        for ln in lines[seen_lines:]:
            print(f"  │ {ln}")
        seen_lines = len(lines)

        # 2) Blocking question → interactive Telegram card + escalation.
        question = state.read_block_question(state_dir)
        if question and question != handled_q:
            print(f"[harn] watch: question raised → routing to {cfg.hil_channel}")
            st = state.State.load(state_dir)
            st.block(question)
            st.save(state_dir)
            reply, source = _await_answer(env_dir, cfg, st.current_task or "(chat)",
                                          question)
            if source == "telegram" and reply is not None:
                answer(env_dir, reply)
                print("[harn] watch: answered via Telegram.")
            elif source == "chat":
                print("[harn] watch: answered in chat.")
            elif source == "auto":
                answer(env_dir, "You pressed 'Decide for me'. " + _AUTO_BUTTON_NOTE)
                print("[harn] watch: delegated to the agent.")
            else:
                notify(f"[harn] Agent needs your input:\n{question}")
            handled_q = question
        elif not question:
            handled_q = None

        # 3) Tasks in review without an oracle verdict → run oracle headless.
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
