# Agent Builder + Generation Design

## Goal

Give Studio a visual way to build an agent (a role, per the agent-roles spec) — its persona, the board status it services, and its **flow assembled from the existing visual workflow canvas** — plus a "describe it and let the model build it" path: from a natural-language description the model drafts the agent's workflow and **selects from the project's existing skills and tools**, presented in the UI for the human to edit and confirm before anything is saved. This closes two gaps: agents are currently hand-edited markdown files with no UI, and there is no "no-implicit-behavior, assemble-the-loop-visually" builder.

## Depends on

- Agent roles spec (`2026-07-18-agent-roles-design.md`) — an "agent" IS a role (`harn_env/agents/<name>.md`); this spec builds the UI + generator for creating them.
- Existing workflow canvas (Flow tab) — a role's `workflow:` field already points at a visually-editable workflow preset (`workflows.create`/`save`/`load`, the Flow canvas). The agent's loop steps ARE that workflow; this spec makes the link explicit and buildable.
- `skills.discover`/`skills.index` and `tools.discover`/`tools.index` — the catalogs the generator selects from.

## Problem

Roles today are created by hand-writing `harn_env/agents/<name>.md` frontmatter (status/next_status/workflow/oracle/secrets/isolation/agent/model) — spec 2 explicitly deferred any UI. There is also no way to say "I need an agent that triages support tickets" and get a starting point. The workflow *steps* of a role's run are already visually editable (the Flow canvas the `workflow:` field points at), but assembling the whole agent — persona + status wiring + which flow + which skills/tools its steps require — is invisible and manual.

## Behavior

### Agents tab (visual builder)
- A new **Agents** tab in Studio, mirroring the Skills/Tools tab pattern. Lists every role from `roles.discover(env_dir)`; selecting one opens an editor.
- The editor exposes every role field as a real control: `name`, `command`, `status` (dropdown from `tasks.lifecycle`), `next_status` (same), `trigger` (auto/manual), `oracle` (toggle), `isolation` (main/worktree), `secrets` (multi-name input, names only), `agent`/`model` (dropdowns from the adapter catalog), and the `## Role` persona body (textarea).
- `workflow:` is a dropdown of existing workflow presets **plus an "Open in Flow canvas" button** — the agent's loop is edited in the same visual canvas that already exists, no second editor. Creating a role can create a new named preset for it.
- Save writes `harn_env/agents/<name>.md` via a new `agents.save`/studio payload; delete removes it. No implicit fields — what the UI shows is exactly the file.

### Generate-from-prompt
- A "Generate agent" input on the Agents tab: the human types a description (e.g. "reviews incoming bug reports, reproduces them, files a fix task").
- `POST /api/agents/generate` runs **one LLM turn** (model configurable, defaulting to the main agent/model) whose prompt contains: the description, the existing **skills index**, the existing **tools index**, the board statuses, and the available adapters — and is instructed to output a strict JSON draft: a role (name/command/status/next_status/oracle/isolation/persona) plus a **workflow** (ordered steps, each step referencing skill names and tool names **chosen only from the provided catalogs**).
- The draft is **validated**: any skill/tool name the model invented that is not in `skills.discover`/`tools.discover` is dropped and reported (never silently invents a capability); an unknown status falls back to the pipeline's first status. The generator never writes files itself.
- The validated draft is returned to the Agents tab and rendered in the SAME editor controls as a manual agent — the human edits freely and only an explicit **Save** persists `agents/<name>.md` + the workflow preset. This keeps the "no implicit behavior" guarantee: nothing runs or persists without human confirmation.

## Implementation

- `harn/agents.py` (extend the discovery module from spec 2, or a sibling): `save(env_dir, role_dict) -> Path`, `delete(env_dir, name) -> bool` — write/remove `agents/<name>.md` from a normalized dict, round-tripping the same frontmatter `roles.discover` parses.
- `harn/agentgen.py` (new): `generate(env_dir, cfg, description) -> dict` — builds the catalog-grounded prompt, runs the turn via `loop.get_adapter`, parses the JSON draft, validates skill/tool/status names against the live catalogs, returns `{role: {...}, workflow: {nodes: [...]}, dropped: [...]}`. Pure — never writes; the caller (studio) persists only on human Save.
- `harn/studio.py`: an Agents tab in `_HTML` (list + editor + generate box, following the Skills-tab JS pattern), and payloads `agents_payload` (list), `save_agent_payload`, `delete_agent_payload`, `generate_agent_payload`, wired as `GET /api/agents`, `POST /api/agents/save`, `POST /api/agents/delete`, `POST /api/agents/generate`. The generated workflow is saved through the existing `workflows.create`/`save` so the Flow canvas can open it.

## Out of scope

- Generating NEW skills or tools — the generator only SELECTS from existing ones (drafting a new custom tool is the separate existing tool-drafting chat).
- Running the generated agent — launching is the agent-roles/triggers surface, already built.
- Versioning/history of agent definitions — the file on disk is the source of truth, same as skills.

## Verification

- Unit (`agents.save`/`delete`): a role dict round-trips to `agents/<name>.md` and back through `roles.discover` unchanged; delete removes it.
- Unit (`agentgen.generate`): a stubbed model reply produces a validated draft; skill/tool names not in the catalog are dropped and listed in `dropped`; an unknown status falls back to the first pipeline status; a malformed/empty model reply returns a safe empty-ish draft, never raises.
- Unit (studio payloads): save/delete/list round-trip; `generate_agent_payload` never writes a file (assert no `agents/*.md` created by a generate call); save persists both the role file and its workflow preset.
- Unit (`_HTML` regression): the Agents tab, its editor controls, and the generate box are present in the served JS.
- Browser (Playwright): create an agent via the UI, generate one from a description (assert the draft fills the editor and only Save persists it), open its flow in the canvas.
