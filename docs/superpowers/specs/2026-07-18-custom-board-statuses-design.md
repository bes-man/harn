# Custom Board Statuses Design

## Goal

Let a project define its own task pipeline (e.g. `new → analyzing → analyzed → developing → done`) instead of the hardcoded 5-status lifecycle, so each stage can later be serviced by a dedicated agent role (see the companion agent-roles spec). Lay the data-shape foundation for external tracker sync (GitLab Issues / Linear / ClickUp) without building any connector.

## Problem

`tasks.py` hardcodes `LIFECYCLE = [todo, in_progress, review, changes_requested, done]` and derives machinery from it (`_NEEDS_AGENT`, `_PICK_RANK`, `set_status` validation, board rendering, Studio columns). An agent conveyor keyed to statuses cannot express "analyst owns `analyzing`, developer owns `developing`" within these five.

## Behavior

- `harn.toml` may declare an ordered status list. Two accepted forms:

```toml
[board]
statuses = ["new", "analyzing", "analyzed", "developing", "done"]
```

or, when a status needs metadata (external-tracker state mapping now; more later):

```toml
[[board.status]]
name = "analyzing"
external = "In Analysis"   # optional: the tracker's state name
```

- **No config → exactly today's five statuses.** Full backward compatibility; existing projects see zero change.
- First status = where new tasks land; last status = terminal ("done" semantics: excluded from agent pickup, counted as complete).
- `set_status` validates against the configured list. Tasks holding a status that was removed from config stay readable (board shows them in an "unknown" bucket) — config edits must never corrupt existing tasks.
- Studio board renders columns from the configured list dynamically.
- Task frontmatter gains an optional `external` block (provider/id/url) — the local mirror of an externally-tracked task, per the already-agreed pull-to-local model. No sync logic in this spec; shape only.

## Machinery mapping

Status semantics that today are hardcoded move to derivation:

- "needs an agent" (today `_NEEDS_AGENT`) — any non-terminal status; refined by the agent-roles spec (a status with an assigned agent).
- Oracle/review gating — no longer keyed to a magic `review` status; moves to per-agent config (companion spec). With no custom config, the built-in five keep their exact current semantics, including `review`'s oracle trigger.

## Implementation

- `tasks.py`: `lifecycle(env_dir)` replaces the module constant everywhere (config-loaded, cached per call site the way `Config.load` already is); keep `LIFECYCLE` as the default fallback.
- `config.py`: parse both `[board]` forms into one normalized list of `{name, external}`.
- Studio: board payload includes the status list; columns render from it.

## Verification

- No-config projects: full existing suite stays green unchanged (the real compatibility proof).
- Unit tests: both config forms parse; set_status validates against custom list; removed-status tasks still load and render; first/last status semantics (new-task landing, terminal exclusion).
- Studio `_HTML` regression test: columns come from the payload, not a hardcoded list.
