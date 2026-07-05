"""Human-readable notes for the studio's Tools tab — what/why/when for each MCP
tool, in plain language.

These are DELIBERATELY separate from the tools' actual docstrings
(`mcp_server.tool_catalog()`). The docstrings are what the agent reads on every
`tools/list` call, so they stay short — harn keeps the agent's fixed context
budget lean (see `tests/test_guidance.py::test_fixed_overhead_under_target`).
A human browsing the studio has no such constraint and needs more: not just
what a tool returns, but why it exists and when to reach for it. Widening the
real docstrings to satisfy both audiences would cost every future agent turn
to satisfy a human occasionally reading the UI — not a good trade. So this
file carries the long-form version; `tools_catalog_payload` (studio.py) merges
it with the real docstring, which stays visible underneath for reference.

Keyed by tool name. Missing an entry just means the raw docstring is shown
alone — never a hard requirement to keep this in sync on every new tool.
"""
from __future__ import annotations

NOTES: dict[str, str] = {
"list_skills": """\
What it does: Returns every installed skill as a one-line "name: description" \
index — never the full body.
Why it's needed: Skill bodies can be long; loading all of them every turn \
would blow the context budget. This index is the cheap, always-safe way to \
scan what exists before deciding what's relevant.
When to use it: At the start of a task, or whenever you're unsure which \
skills exist for a domain — before calling read_skill on the ones that look \
relevant.""",

"read_skill": """\
What it does: Returns the full Markdown body of ONE named skill.
Why it's needed: Skill content (standards, conventions, gotchas) only enters \
your context when you explicitly ask for it — that's the whole point of the \
skill system: relevant knowledge on demand, not a wall of text every turn.
When to use it: Right before you touch a domain that skill covers (e.g. \
"testing" before writing tests, "security" before touching auth). Don't call \
it speculatively for skills the task doesn't need.""",

"save_to_skill": """\
What it does: Appends a fact, convention, or standard to a skill file \
(creating the skill if it doesn't exist yet).
Why it's needed: This is how harn accumulates project knowledge across \
sessions — a decision made once should never need re-deriving or re-asking \
next time. Without this, every task would start from zero.
When to use it: When you learn something that will matter to FUTURE tasks — \
a human's answer to a design question, a convention you discovered in the \
codebase, a standard the team follows. Confirm with the user first if it's \
a judgment call, not an obvious fact.""",

"read_guidance": """\
What it does: Returns one on-demand protocol topic (services, code-search, \
hil, parallel, design, browser, onboarding, tasks) that isn't in the \
always-loaded AGENTS.md.
Why it's needed: Some protocol detail only matters in specific situations \
(e.g. how to run parallel agents, or the human-in-the-loop escalation \
flow) — injecting it into every turn's prompt would waste tokens on tasks \
that never touch it.
When to use it: When the CURRENT task's situation matches one of the \
topics — e.g. read_guidance("parallel") before fanning out independent \
tasks to multiple agents.""",

"list_services": """\
What it does: Returns the index of registered services/modules — name + \
one-line responsibility for each.
Why it's needed: This is the persisted "AS IS" map of the codebase (what \
owns what) so agents don't have to re-explore the repo from scratch every \
task to figure out which module a change belongs to.
When to use it: Early in a task, to decide which parts of the system it \
touches — then call read_service only on those, instead of grepping the \
whole repo.""",

"read_service": """\
What it does: Returns the full knowledge file for ONE service/module: its \
responsibility, the standards changes to it must follow, hard constraints, \
and known gotchas.
Why it's needed: Once a service is understood and documented, future tasks \
touching it shouldn't have to re-discover its rules from scratch by reading \
all the code again.
When to use it: For any service the current task actually modifies or \
depends on — not a general codebase tour.""",

"save_service": """\
What it does: Registers or fully replaces a service's knowledge file \
(responsibility + standards + constraints + gotchas).
Why it's needed: This is the write side of the AS-IS map — after exploring \
a service for a task, capturing what you learned means the NEXT task that \
touches it skips re-discovery entirely.
When to use it: After you've understood a service well enough to describe \
its rules (not a code walkthrough — the constraints and gotchas a change \
must respect). Read it first if it already exists, so you don't drop true \
facts on overwrite.""",

"ensure_skill": """\
What it does: Installs an industry-baseline skill (frontend, backend, api, \
testing, security, accessibility, performance, database) for a domain the \
project has no skill for yet, and returns its body.
Why it's needed: A brand-new project has no accumulated conventions — this \
seeds a sane starting point instead of the agent inventing standards ad hoc \
or leaving a domain completely uncovered.
When to use it: When a task touches a domain flagged as a "skill gap" \
(harn tells you this in get_next_task's output) and no project-specific \
skill exists yet. Follow up with save_to_skill once you learn project-\
specific deviations from the baseline.""",

"get_next_task": """\
What it does: Atomically claims and returns the highest-priority runnable \
task (skipping ones with unmet dependencies or already claimed by another \
worker), plus context the task needs (service index, skill-gap warnings, \
autonomy level).
Why it's needed: This is the single entry point into harn's work queue — it \
guarantees two parallel agents never grab the same task, and that resuming \
your own in-progress task just works.
When to use it: At the start of every work session, to pick up what to do \
next. Pass a stable `worker` id if you're running multiple agents in \
parallel so each resumes its own task correctly.""",

"runnable_tasks": """\
What it does: Lists every task that could start RIGHT NOW (dependencies \
met, not claimed) WITHOUT claiming any of them.
Why it's needed: Before committing to one task, it's useful to know how \
much independent work exists — 2+ runnable tasks means real parallelism is \
available, not just a queue to work one at a time.
When to use it: When deciding whether to suggest fanning out work across \
multiple agents/windows, before anyone calls get_next_task.""",

"create_task": """\
What it does: Authors a new task as a JSON file in harn_env/tasks/, with a \
title, Markdown description (What + Done when), and metadata (priority, \
PRD links, skills hint, dependencies, workflow preset).
Why it's needed: Every code change in harn is tracked as a task — this is \
how ad-hoc requests ("fix this", "add that") become a first-class, \
resumable, reviewable unit of work instead of untracked chat output.
When to use it: Whenever the human asks for a change with no existing \
matching task — even a one-liner. Clarify with ask_user first if the \
request is vague; write concrete, checkable "Done when" criteria.""",

"update_task": """\
What it does: Updates specific fields of an existing task (title, \
description, skills, prds, priority) — only the fields you pass are \
changed; harn-managed fields (status, review_log, id) are untouched.
Why it's needed: Planning often narrows or corrects a task's scope after \
it's created — this lets the spec evolve without losing its history.
When to use it: Mainly during planning, to write refined "## What" / \
"## Done when" criteria back into the task once clarified.""",

"read_prd": """\
What it does: Returns a PRD's full text on demand (harn_env/prd/<slug>.md).
Why it's needed: PRDs carry the WHY and full scope behind a task, but \
injecting the whole document into every turn wastes tokens once the spec \
is locked — the task's own distilled criteria are usually enough after that.
When to use it: During PLANNING, to understand context before narrowing \
the task. Rarely needed again after lock_spec — by then the task already \
carries what matters.""",

"lock_spec": """\
What it does: Closes the clarification funnel — writes the final, \
unambiguous acceptance criteria ("Done when") into the task, plus optional \
chosen approach and recorded decisions, and marks the spec locked.
Why it's needed: This is the hinge between planning and execution: once \
locked, the executor and the oracle both trust this spec as authoritative, \
so the FULL PRD no longer needs re-reading every turn — real token savings \
with no loss of accuracy, because ambiguity is already resolved.
When to use it: At the END of planning, once your clarifying questions \
have actually eliminated the ambiguity — never before, and never as a way \
to skip clarifying.""",

"record_decision": """\
What it does: Records one non-obvious choice you made (a library, an \
approach, a trade-off, an assumption) along with the real reason for it.
Why it's needed: Decisions carry forward to your next turn on this task \
(so you don't re-derive them), AND the oracle verifies each one against \
the acceptance criteria — a decision with a shaky rationale is exactly \
what independent review should catch.
When to use it: Whenever you make a call the human didn't explicitly \
specify and a future reader would wonder "why this way?" State the ACTUAL \
reason, not a retroactive justification for a shortcut.""",

"record_change": """\
What it does: Logs one significant, release-notes-style line about what \
shipped in this task, plus optional detail on decisions/standards it set.
Why it's needed: This is the raw material for generate_changelog — a \
readable release history assembled after the fact, without anyone having \
to reconstruct "what actually changed" from commit messages or memory.
When to use it: After finishing a MEANINGFUL piece of work — not a diary \
of every edit. One line, user-facing voice, describing what changed and \
why it matters.""",

"generate_changelog": """\
What it does: Assembles a Markdown changelog from every task's recorded \
changes and decisions, either for one task or the whole project.
Why it's needed: This turns the scattered record_change entries + \
decisions into a coherent, presentable release-notes document on demand — \
without maintaining a separate hand-written changelog.
When to use it: When the human asks for release notes, a changelog, or a \
summary of what was built. Pass write=true to also save it to \
harn_env/CHANGELOG.md.""",

"set_scratchpad": """\
What it does: Saves a short free-form note that carries forward to your \
NEXT turn on this task, replacing whatever was there before.
Why it's needed: Some context is working memory, not a permanent record — \
"what's done, what's left, a gotcha I hit" — useful for continuity but not \
something the oracle should treat as an acceptance criterion.
When to use it: At the end of a turn on a task that isn't finished yet, so \
the next turn (yours or another agent's) picks up exactly where you left \
off instead of re-exploring.""",

"board": """\
What it does: Shows every task and its current status on the track \
(todo → in_progress → review → changes_requested → done).
Why it's needed: This is the at-a-glance view of the whole backlog's state \
— what's finished, what's blocked, what's next — without opening every \
task file individually.
When to use it: Before starting work, to see what's already done and \
planned, and to spot dependencies or overlaps with what you're about to do.""",

"ask_user": """\
What it does: Persists a clarifying question — sets BLOCKED state, \
escalates to Telegram if the human doesn't answer in chat soon, and (if a \
skill is named) promotes the eventual answer into that skill automatically.
Why it's needed: This is harn's human-in-the-loop mechanism — it makes \
sure a genuinely ambiguous or high-stakes decision actually reaches the \
human, across chat AND headless runs, instead of the agent silently \
guessing or stalling with no visible signal.
When to use it: For anything BOTH ambiguous AND significant or hard to \
undo — write the question EXPANDED (context, 2-3 options with trade-offs, \
your recommendation). Surface it in your client's native UI FIRST (e.g. \
AskUserQuestion), then call this tool in the same turn, then stop.""",

"answer_question": """\
What it does: Records the human's answer to the most recent ask_user \
question — clears BLOCKED, saves the answer for future reference, promotes \
it into a skill if one was named, and cancels any pending Telegram \
escalation.
Why it's needed: This closes the loop opened by ask_user so work can \
resume, and makes sure the answer is captured durably rather than living \
only in the chat transcript.
When to use it: Right after the human replies to a question you raised \
with ask_user — then continue the task.""",

"check_pending_answer": """\
What it does: Checks whether a pending ask_user question was already \
answered via Telegram or a "Decide for me" button while you were stopped.
Why it's needed: A human might answer through a channel other than the \
current chat (e.g. Telegram, while away from their computer) — this lets \
you pick that answer up instead of asking again or blocking forever.
When to use it: On resume, when the human's message is ambiguous ("ok", \
"continue") rather than an actual answer to your question — check here \
before assuming they mean something specific.""",

"save_design": """\
What it does: Saves a single self-contained HTML mockup as the visual \
contract for a user-facing task, to harn_env/design/<task_id>.html.
Why it's needed: For UI work, agreeing on the LOOK before writing code \
avoids expensive rework — the mockup becomes what the executor builds to \
and what the oracle/browser-verify step checks the real UI against.
When to use it: During planning for any user-facing task, before \
implementation starts. Include empty/error/success states, not just the \
happy path. Get human approval via ask_user before coding.""",

"read_design": """\
What it does: Returns the approved HTML mockup for a task, or a note if \
none exists.
Why it's needed: Implementation and verification both need the SAME \
visual reference — reading it here (rather than re-describing it in \
prose) keeps that reference exact.
When to use it: Before implementing or verifying any user-facing task \
that has a design mockup.""",

"run_tests": """\
What it does: Runs the project's configured test command and returns the \
tail of its output.
Why it's needed: This is the objective, automatic gate between "I wrote \
code" and "the code works" — harn runs it as part of every task cycle so \
review isn't the first time correctness gets checked.
When to use it: After making a code change, before considering a task done \
or submitting it for review.""",

"submit_for_review": """\
What it does: Marks a task as finished and ready for HUMAN review (status \
→ review) — it does NOT mark it done; only the human's acceptance does that.
Why it's needed: This is the explicit handoff point from agent work to \
human judgment — the task stops being "in progress" without silently \
becoming "accepted."
When to use it: After tests pass and you believe the task is complete. \
harn also does this automatically at the end of a turn, so calling it \
yourself is an optional, explicit signal with a summary of what you did.""",

"reconcile_skills": """\
What it does: Returns a brief on what a just-finished task should teach — \
changed files, existing skills that might need updating, and instructions \
to save confident conventions, ask the human about trade-offs, and refresh \
touched service files.
Why it's needed: This is how harn's knowledge actually compounds across \
tasks — without this step, insights from finishing a task would evaporate \
instead of making the NEXT task smarter.
When to use it: Right after submit_for_review, as the last step of a \
task's work cycle.""",

"loop_status": """\
What it does: Returns harn's current loop phase, the active task, and any \
pending question.
Why it's needed: This is a quick way to check "where is harn right now" — \
distinct from an OS-level healthcheck, it's specifically the ralph-loop's \
own state machine.
When to use it: When you need to confirm the current phase or task before \
acting, e.g. after resuming a session.""",

"explain_pipeline": """\
What it does: Shows which pipeline stages (plan, execute, test, verify, \
ui_verify, oracle, reconcile) will run for the CURRENT config, with \
gated-off ones clearly marked.
Why it's needed: Gives the human predictability — what's about to happen \
— before a run starts, instead of discovering mid-run that e.g. the \
oracle stage is disabled.
When to use it: When the human asks what a run will do, or you want to \
confirm which stages are active before starting one.""",

"read_workflow": """\
What it does: Returns harn_env/WORKFLOW.md — the always-followed flow for \
this project, with each step's required skills, plus a footer listing any \
named workflow presets a task can run under.
Why it's needed: WORKFLOW.md is the single file every agent (Claude, \
Codex, Cursor, …) reads to know the process to follow — reading it is how \
you learn what's expected at each stage before improvising your own.
When to use it: At the start of every session, before doing anything else.""",

"save_workflow": """\
What it does: Overwrites harn_env/WORKFLOW.md with new content.
Why it's needed: The workflow is meant to be co-authored with the human \
(e.g. during onboarding) and then followed consistently — this is how \
that agreed process gets persisted for every future agent to read.
When to use it: When you and the human have agreed on a new or updated \
process — keep the `## N. Step` + `Skills (required: …)` shape so harn \
can still parse mandatory skills per step.""",

"set_task_workflow": """\
What it does: Assigns a named workflow preset to a task (or clears it back \
to the project default) — the loop renders that preset into WORKFLOW.md \
the next time the task is picked up.
Why it's needed: Different tasks can need different processes (e.g. a \
"QA sprint" preset with a test-heavy flow vs. the default) — this is how \
one task opts into a different flow without changing the project-wide \
default for every other task.
When to use it: When a task explicitly needs a non-default process. \
Preset names are listed in read_workflow's footer or the studio UI's \
Board tab.""",

"save_attachment": """\
What it does: Saves a base64-encoded file (typically an image) to a \
task's attachment folder (harn_env/storage/<task_id>/). A name collision \
never overwrites — it auto-suffixes ("design (1).png") so nothing is lost.
Why it's needed: A task's context is more than text — a design reference \
the human pastes in, or a diagram/screenshot you generate, needs somewhere \
durable to live that survives beyond the current conversation and is \
visible to the human (studio Board) and any later agent.
When to use it: When the human shares an image/file for this task, or you \
generate one worth keeping (e.g. a diagram). Check list_attachments first \
if you're unsure whether something similar already exists.""",

"list_attachments": """\
What it does: Lists a task's saved attachments — filename, size, and \
whether each is an image or a generic file.
Why it's needed: Before generating or re-saving something, it's worth \
knowing what's already attached — avoids duplicate uploads and lets you \
reference existing material by name.
When to use it: At the start of a task that might have design references \
or prior attachments, or before calling save_attachment to check for an \
existing file with the same purpose.""",

"read_attachment": """\
What it does: Returns one attachment by name. If it's an image, you get \
back REAL image content — you actually see it, the same as if the human \
had pasted it into chat. Non-image files come back as their raw text (or \
a note if they're binary and unreadable as text).
Why it's needed: A design reference is only useful if the agent can \
actually SEE it, not just know a file exists — this is what makes \
save_attachment's images actionable rather than inert.
When to use it: Before implementing or verifying any task that has a \
design reference or other visual material attached — call \
list_attachments first if you don't already know the filename.""",
}


def merged(name: str, docstring: str) -> str:
    """The docstring plus its long-form UI note, if one exists — never
    replaces the docstring (single source of truth for what the tool
    actually does), only adds the why/when framing around it."""
    note = NOTES.get(name)
    if not note:
        return docstring
    return f"{note}\n\n— agent-facing docstring —\n{docstring}"
