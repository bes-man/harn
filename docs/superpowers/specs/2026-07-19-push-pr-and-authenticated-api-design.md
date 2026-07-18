# Push → PR + Authenticated API Design

## Goal

Two things that make the "agent finishes work and hands it back for review" loop real and safe: (1) a successful agent run can **commit its work to a new branch, push, and open a GitHub PR against a configurable base branch** (e.g. `dev` for a deploy-from-dev flow), with the merge left to a human; (2) the agent HTTP API is protected by a **Bearer token from `secrets.env`**, and the endpoints are **documented**.

## Depends on

- Agent roles / triggers specs — the push/PR step runs at the end of `roles_runner.run_role`; the authenticated routes are the `/api/agents/*` and `/api/tasks/intake` surface.
- `secrets_store` — the API token lives in `harn_env/secrets.env` (already chmod-600, gitignored).
- `gitutil` — extended with the first real commit path (harn only stash-checkpoints today; it never commits to a branch).

## Problem

- harn never commits to a branch — the Ralph loop leaves changes in the working tree (patches + stash checkpoints). So "commit, push, open a PR" cannot happen automatically; a human must do it by hand after every run.
- The Studio HTTP server (including `POST /api/agents/run`) has **no authentication at all** — the only protection is binding `127.0.0.1`. There is no way to safely expose agent invocation to an external caller, and no documentation of the API.

## Behavior

### Push → PR (opt-in per agent)
- A role frontmatter flag `push: true` (default `false`) turns on the git stage. After the role's run succeeds — workflow steps done, oracle passed (or `oracle: false`), status transitioned — harn:
  1. creates a branch `<branch_prefix><task-id>` from the current HEAD (prefix configurable, default `harn/`),
  2. commits the task's diff (the working-tree changes the run produced) with a message derived from the task title + id,
  3. pushes the branch to `origin`,
  4. runs `gh pr create --base <pr_base> --head <branch> --title … --body …` (PR body = the task's `## Result` + a link back to the task id).
- **The merge is always human.** harn opens the PR and reports its URL (into the task `## Result` and the completion callback — Telegram reply / API response); it never merges.
- Config: `[git] pr_base` (default = the repo's default branch; set to `dev` to target a deploy branch), `[git] branch_prefix` (default `harn/`), `[git] push_remote` (default `origin`). If `gh` is not installed or not authenticated, the push still happens and harn reports "branch pushed, PR not created (gh unavailable)" — never fails the whole run over PR creation.
- Provider seam: a thin `prhost` interface (`create_pr(base, head, title, body) -> url | None`) with a `gh` implementation registered; GitLab/others are future one-file providers (mirrors `trackers.py`). Only `gh` is implemented here.

### Authenticated API
- If `HARN_API_TOKEN` is set in `secrets.env`, the sensitive routes — `/api/agents/run`, `/api/agents/generate`, `/api/tasks/intake` — require `Authorization: Bearer <token>`, EXCEPT for requests originating from loopback (`127.0.0.1`/`::1`), so the local Studio browser UI keeps working with no token (it is already the trusted local operator). A non-loopback request without a valid token gets `401`.
- If `HARN_API_TOKEN` is unset, behavior is exactly as today (localhost-only, no token) — full backward compatibility; nothing breaks for existing local users.
- Token comparison is constant-time (`hmac.compare_digest`). The token value is never logged, never echoed in a response, never sent to Telegram.

### Documentation
- A real **"Agent API" section in README** + a `docs/agent-api.md` reference: every endpoint (`/api/agents/run`, `/api/agents/generate`, `/api/tasks/intake`), the auth model (Bearer token, loopback exemption, how to set `HARN_API_TOKEN`), request/response shapes, and `curl` examples. Closes the "the API is undocumented" gap.

## Implementation

- `harn/gitutil.py`: `commit_to_branch(cwd, branch, message, exclude=()) -> str | None` — create/checkout the branch, stage the task's changes, commit, return the commit sha (best-effort; returns None and touches nothing on any failure, so a non-git or dirty-unexpected state can never corrupt the working tree). `push_branch(cwd, remote, branch) -> bool`.
- `harn/prhost.py` (new): `PRHost` interface + `GhPRHost` (`subprocess` `gh pr create`, parses the printed URL) + a null host; `create_pr(...)` dispatches on config.
- `harn/roles_runner.py`: after a successful transition, if `role.push`, run the branch→commit→push→PR stage, writing the PR URL into `## Result` and returning it in the run result. Guarded so a failure here never rolls back the (already-succeeded) task work.
- `harn/studio.py`: a `_require_auth(handler, route)` check at the top of `do_POST`/`do_GET` for the sensitive routes — reads `secrets_store.load(env_dir).get("HARN_API_TOKEN")`, checks the `Authorization` header with `hmac.compare_digest`, exempts loopback (`self.client_address[0]`). Returns 401 otherwise.
- `harn/config.py`: `[git] pr_base`/`branch_prefix`/`push_remote`.
- `README.md` + `docs/agent-api.md`: the reference.

## Out of scope

- Auto-merging PRs — always human.
- GitLab / Bitbucket PR creation — `prhost` seam only; `gh` (GitHub) is the one implementation.
- Per-user tokens / OAuth / token rotation — a single shared `HARN_API_TOKEN` (matches the single-operator model). Rotation = edit `secrets.env`.
- Signing commits, conventional-commit enforcement — the commit message is title + id, nothing more.

## Verification

- Unit (`gitutil.commit_to_branch`/`push_branch`): commits the working-tree diff onto a fresh branch and returns a sha (in a temp git repo); a non-repo / failure path returns None and leaves the tree untouched.
- Unit (`prhost`): `GhPRHost.create_pr` builds the correct `gh` argv and parses the URL from stubbed output; a missing/unauthenticated `gh` returns None without raising.
- Unit (`roles_runner`): `push: true` runs the git stage after a successful transition and records the PR URL in `## Result`; `push: false` (default) skips it entirely; a PR-creation failure does NOT fail the task run.
- Unit (studio auth): with `HARN_API_TOKEN` set, a non-loopback request to `/api/agents/run` without a valid Bearer token returns 401; with the correct token it passes; a loopback request passes with no token; with `HARN_API_TOKEN` unset, all routes behave as today. Constant-time comparison used (assert `hmac.compare_digest` is the check).
- Unit (`config`): `pr_base`/`branch_prefix`/`push_remote` parse with correct defaults (`pr_base` = repo default branch).
- Docs: `docs/agent-api.md` exists and lists all three endpoints + the auth model (a test asserting the file exists and mentions each route is enough).
