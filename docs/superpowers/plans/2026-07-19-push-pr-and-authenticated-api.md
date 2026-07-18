# Push → PR + Authenticated API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A role run can commit its work to a branch, push, and open a GitHub PR against a configurable base (human merges); and the agent API is gated by a Bearer token from secrets.env (loopback-exempt), with real docs.

**Architecture:** Config first (`[git]`), then the git primitives (`commit_to_branch`/`push_branch` — harn's first real commit path, which today only stash-checkpoints), then the PR-host seam (`prhost.py`, gh implementation), then the role push→PR stage (opt-in `push:` role flag) wired into `roles_runner`, then the Studio Bearer-auth gate on the sensitive routes, then docs. Each git/auth piece is unit-tested (temp git repo, stubbed subprocess, hmac check).

**Tech Stack:** Python stdlib (`subprocess`, `hmac`), the `gh` CLI (external, degrades gracefully if absent), existing harn conventions. No new pip dependency.

## Global Constraints

- No new third-party pip dependency (`gh` is an external CLI, optional — its absence must never fail a run).
- harn must NEVER auto-merge a PR — it opens the PR and reports the URL; merge is always human.
- The git commit path is opt-in per role (`push: true`, default false); the default loop and non-push roles behave exactly as today (harn never commits).
- Auth backward-compat: if `HARN_API_TOKEN` is unset, ALL routes behave as today (localhost-only, no token). When set, only the sensitive routes require the token, and loopback requests are exempt so the local browser UI keeps working with no token.
- Token comparison uses `hmac.compare_digest` (constant-time); the token value is never logged, echoed, or sent to Telegram.
- Follow `docs/superpowers/specs/2026-07-19-push-pr-and-authenticated-api-design.md`.

---

### Task 1: `[git]` config section

**Files:**
- Modify: `harn/config.py`
- Test: `tests/test_push_pr.py` (new)

**Interfaces:**
- Produces: `Config.git_pr_base: str` (default `""` = resolve to the repo default branch at use time), `Config.git_branch_prefix: str` (default `"harn/"`), `Config.git_push_remote: str` (default `"origin"`), parsed from `[git]` in harn.toml.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_push_pr.py
"""Push→PR + authenticated API spec (docs/superpowers/specs/2026-07-19-push-pr-and-authenticated-api-design.md)."""
from __future__ import annotations
from harn.config import Config
from harn import ENV_DIRNAME


def _env(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    return env


def test_git_config_defaults(tmp_path):
    cfg = Config.load(_env(tmp_path))
    assert cfg.git_pr_base == ""
    assert cfg.git_branch_prefix == "harn/"
    assert cfg.git_push_remote == "origin"


def test_git_config_from_toml(tmp_path):
    env = _env(tmp_path)
    (env / "harn.toml").write_text(
        '[git]\npr_base = "dev"\nbranch_prefix = "bot/"\npush_remote = "upstream"\n',
        encoding="utf-8")
    cfg = Config.load(env)
    assert cfg.git_pr_base == "dev"
    assert cfg.git_branch_prefix == "bot/"
    assert cfg.git_push_remote == "upstream"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_push_pr.py -v`
Expected: FAIL — `Config` has no attribute `git_pr_base`.

- [ ] **Step 3: Implement**

In `harn/config.py`: add `"git": {"pr_base": "", "branch_prefix": "harn/", "push_remote": "origin"}` to `DEFAULTS`; add fields to `Config`:
```python
    git_pr_base: str = ""
    git_branch_prefix: str = "harn/"
    git_push_remote: str = "origin"
```
and in `Config.load`'s return:
```python
            git_pr_base=str((data.get("git", {}) or {}).get("pr_base", "") or "").strip(),
            git_branch_prefix=str((data.get("git", {}) or {}).get("branch_prefix", "harn/") or "harn/").strip(),
            git_push_remote=str((data.get("git", {}) or {}).get("push_remote", "origin") or "origin").strip(),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_push_pr.py -v`
Expected: 2 passed.

- [ ] **Step 5: Full suite** — `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/config.py tests/test_push_pr.py
git commit -m "feat(config): [git] pr_base/branch_prefix/push_remote settings"
```

---

### Task 2: `gitutil.commit_to_branch` + `push_branch`

**Files:**
- Modify: `harn/gitutil.py`
- Test: `tests/test_push_pr.py`

**Interfaces:**
- Produces: `commit_to_branch(cwd, branch, message, exclude=()) -> str | None` — creates/checks out `branch` from current HEAD, stages the working-tree changes (honoring `exclude` pathspecs), commits, returns the commit sha; returns None and leaves the tree untouched on any failure or if there is nothing to commit. `push_branch(cwd, remote, branch) -> bool`. Both best-effort like the rest of gitutil (never raise).

- [ ] **Step 1: Write the failing tests**

```python
import subprocess
from harn import gitutil


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "base"], tmp_path)
    return tmp_path


def test_commit_to_branch_commits_working_tree_changes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    sha = gitutil.commit_to_branch(repo, "harn/PRJ-1", "work on PRJ-1")
    assert sha
    # on the new branch, both changes committed
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=repo, capture_output=True, text=True).stdout.strip()
    assert branch == "harn/PRJ-1"
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=repo,
                        capture_output=True, text=True).stdout
    assert "work on PRJ-1" in log


def test_commit_to_branch_nothing_to_commit_returns_none(tmp_path):
    repo = _repo(tmp_path)
    assert gitutil.commit_to_branch(repo, "harn/PRJ-2", "noop") is None


def test_commit_to_branch_non_repo_returns_none(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert gitutil.commit_to_branch(d, "b", "m") is None


def test_push_branch_non_repo_returns_false(tmp_path):
    d = tmp_path / "plain2"
    d.mkdir()
    assert gitutil.push_branch(d, "origin", "b") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "commit_to_branch or push_branch"`
Expected: FAIL — no such functions.

- [ ] **Step 3: Implement**

In `harn/gitutil.py` (reuse the module's `_run` helper and `is_repo`):

```python
def commit_to_branch(cwd: Path, branch: str, message: str,
                     exclude: tuple[str, ...] = ()) -> str | None:
    """Create/checkout `branch` from HEAD, stage the working-tree changes
    (minus `exclude` pathspecs), commit, return the commit sha. Returns None
    (touching nothing) if not a repo, nothing to commit, or any git step
    fails — harn's ONLY commit path, opt-in per role."""
    if not is_repo(cwd) or not branch:
        return None
    # branch may already exist (a re-run) — checkout if so, else create.
    if _run(["rev-parse", "--verify", "--quiet", branch], cwd)[0] == 0:
        if _run(["checkout", branch], cwd)[0] != 0:
            return None
    elif _run(["checkout", "-b", branch], cwd)[0] != 0:
        return None
    pathspec = [".", *[f":(exclude){p}" for p in exclude]]
    _run(["add", "-A", "--", *pathspec], cwd)
    # nothing staged → nothing to commit
    if _run(["diff", "--cached", "--quiet"], cwd)[0] == 0:
        return None
    if _run(["commit", "-m", message], cwd)[0] != 0:
        return None
    code, out, _ = _run(["rev-parse", "HEAD"], cwd)
    return out if code == 0 else None


def push_branch(cwd: Path, remote: str, branch: str) -> bool:
    """Push `branch` to `remote` (best-effort). Returns success."""
    if not is_repo(cwd) or not remote or not branch:
        return False
    return _run(["push", "-u", remote, branch], cwd, timeout=60)[0] == 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "commit_to_branch or push_branch"`
Expected: 4 passed.

- [ ] **Step 5: Full suite** — `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/gitutil.py tests/test_push_pr.py
git commit -m "feat(gitutil): commit_to_branch + push_branch (harn's first commit path, opt-in)"
```

---

### Task 3: `prhost.py` — PR-host seam + gh implementation

**Files:**
- Create: `harn/prhost.py`
- Test: `tests/test_push_pr.py`

**Interfaces:**
- Produces: `create_pr(cwd, *, base, head, title, body) -> str | None` — dispatches to the `gh` implementation; returns the PR URL or None (gh missing/unauthenticated/failed → None, never raises). Internally a `GhPRHost` class building `gh pr create --base … --head … --title … --body …` and parsing the printed URL, plus a null host.

- [ ] **Step 1: Write the failing tests**

```python
from harn import prhost


def test_gh_create_pr_builds_argv_and_parses_url(monkeypatch, tmp_path):
    calls = {}
    class R:
        returncode = 0
        stdout = "https://github.com/o/r/pull/42\n"
        stderr = ""
    def fake_run(argv, cwd=None, capture_output=True, text=True, timeout=None):
        calls["argv"] = argv
        return R()
    monkeypatch.setattr(prhost.subprocess, "run", fake_run)
    url = prhost.create_pr(tmp_path, base="dev", head="harn/PRJ-1",
                           title="PRJ-1", body="did the thing")
    assert url == "https://github.com/o/r/pull/42"
    assert calls["argv"][:3] == ["gh", "pr", "create"]
    assert "--base" in calls["argv"] and "dev" in calls["argv"]
    assert "--head" in calls["argv"] and "harn/PRJ-1" in calls["argv"]


def test_gh_create_pr_failure_returns_none(monkeypatch, tmp_path):
    class R:
        returncode = 1
        stdout = ""
        stderr = "gh: not authenticated"
    monkeypatch.setattr(prhost.subprocess, "run",
                        lambda *a, **k: R())
    assert prhost.create_pr(tmp_path, base="main", head="h", title="t", body="b") is None


def test_gh_create_pr_missing_binary_returns_none(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(prhost.subprocess, "run", boom)
    assert prhost.create_pr(tmp_path, base="main", head="h", title="t", body="b") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "create_pr"`
Expected: FAIL — no module `harn.prhost`.

- [ ] **Step 3: Implement**

Create `harn/prhost.py`:

```python
"""PR-host seam: open a pull request for a pushed branch. `gh` (GitHub)
is the one implementation; GitLab/others are future one-file providers
(mirrors trackers.py). Never raises — a missing/unauthenticated gh returns
None so a role run's PR step degrades to 'branch pushed, no PR'. See
docs/superpowers/specs/2026-07-19-push-pr-and-authenticated-api-design.md.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_URL_RE = re.compile(r"https?://\S+/pull/\d+")


def create_pr(cwd: Path, *, base: str, head: str, title: str, body: str) -> str | None:
    """Open a GitHub PR via `gh pr create`. Returns the PR URL, or None on
    any failure (gh absent, not authenticated, non-zero exit)."""
    argv = ["gh", "pr", "create", "--base", base, "--head", head,
            "--title", title, "--body", body]
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    m = _URL_RE.search(r.stdout or "")
    return m.group(0) if m else (r.stdout or "").strip() or None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "create_pr"`
Expected: 3 passed.

- [ ] **Step 5: Full suite** — `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/prhost.py tests/test_push_pr.py
git commit -m "feat(prhost): gh PR-host seam (create_pr, degrades to None)"
```

---

### Task 4: `Role.push` flag + roles_runner push→PR stage

**Files:**
- Modify: `harn/roles.py` (add `push` field), `harn/roles_runner.py` (push stage)
- Test: `tests/test_push_pr.py`

**Interfaces:**
- Consumes: `gitutil.commit_to_branch`/`push_branch` (Task 2), `prhost.create_pr` (Task 3), `Config.git_*` (Task 1).
- Produces: `Role.push: bool` (default False, frontmatter `push:`); after a role run's successful transition, if `role.push`, `roles_runner` commits→pushes→opens a PR, writes the PR URL into the task's `## Result`, and includes it in the run result. A failure in this stage NEVER fails the (already-succeeded) run.

- [ ] **Step 1: Write the failing tests**

```python
def test_role_push_field_discovered(tmp_path):
    from harn import roles
    env = tmp_path / ENV_DIRNAME
    (env / "agents").mkdir(parents=True)
    roles.save(env, {"name": "dev", "status": "todo", "push": True, "body": "x"})
    r = roles.find(env, "dev")
    assert r.push is True
    roles.save(env, {"name": "dev2", "status": "todo", "body": "x"})
    assert roles.find(env, "dev2").push is False


def test_run_role_push_stage_commits_pushes_opens_pr(tmp_path, monkeypatch):
    import subprocess
    from harn import roles, roles_runner, loop, tasks, workflows, scaffold, ENV_DIRNAME
    from harn.adapters.base import AgentResult

    def _git(args, cwd):
        subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"): p.unlink()
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n[feedback]\ntest_cmd=""\n'
        'require_tests=false\n[loop]\noracle=false\n[notify]\nwait_for_reply=false\n'
        '[git]\npr_base = "dev"\n')
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    from .conftest import make_task
    t = make_task(env, "PRJ-1", title="Feat")
    plan = {"preamble": "", "nodes": [{"kind": "step", "title": "Do", "body": "do",
        "id": "step-1", "agent": "", "model": "", "effort": "", "temperature": "",
        "required": [], "tools": [], "enabled": True}]}
    workflows.save_task_plan(env, t.id, plan)
    (env / "agents").mkdir(exist_ok=True)
    (env / "agents" / "dev.md").write_text(
        "---\nname: dev\nstatus: todo\noracle: false\npush: true\n---\n## Role\ndev", encoding="utf-8")

    # the run makes a file change so there's something to commit
    def fake_turn(prompt, cwd, timeout=1800, *, model=None, effort=None, temperature=None):
        (cwd / "out.txt").write_text("done\n", encoding="utf-8")
        return AgentResult(ok=True, text="done")
    class Fake:
        name = "fake"
        def available(self): return True
        run_turn = staticmethod(fake_turn)
    monkeypatch.setattr(loop, "get_adapter", lambda n: Fake())

    pushed = {}
    monkeypatch.setattr(roles_runner.gitutil, "push_branch",
                        lambda cwd, remote, branch: pushed.setdefault("branch", branch) or True)
    monkeypatch.setattr(roles_runner.prhost, "create_pr",
                        lambda cwd, *, base, head, title, body: pushed.setdefault("base", base) or
                        "https://github.com/o/r/pull/7")

    r = roles_runner.run_role(tmp_path, env, "PRJ-1", "dev")
    assert r["ok"] is True
    assert r.get("pr_url") == "https://github.com/o/r/pull/7"
    assert pushed["base"] == "dev"          # configurable base honored
    assert pushed["branch"].endswith("PRJ-1")
    assert "https://github.com/o/r/pull/7" in tasks.find(env, "PRJ-1").result


def test_run_role_push_pr_failure_does_not_fail_run(tmp_path, monkeypatch):
    # same setup as above but create_pr returns None (gh unavailable) →
    # run still ok, pr_url None, branch still pushed.
    ...  # (implementer: mirror the prior test; assert r["ok"] is True and r.get("pr_url") is None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "push"`
Expected: FAIL — `Role` has no `push`; roles_runner has no push stage.

- [ ] **Step 3: Implement**

In `harn/roles.py`: add `push: bool = False` to the `Role` dataclass; add `"push"` to `_ROLE_FM_FIELDS`; in `discover`, parse `push_raw = fm.get("push", False); push=push_raw if isinstance(push_raw, bool) else False`.

In `harn/roles_runner.py`: import `gitutil` and `prhost`. After the transition block (before the final events_mod.emit/return), add:
```python
    pr_url = None
    if getattr(role, "push", False):
        pr_url = _push_and_open_pr(env_dir, project_root, task, role, cfg)

    events_mod.emit(...)
    return {"ok": True, "task_id": task.id, "status": task.status,
            "warning": warning, "pr_url": pr_url}
```
and add the helper (guarded so nothing here can fail the run):
```python
def _push_and_open_pr(env_dir, project_root, task, role, cfg):
    try:
        branch = f"{cfg.git_branch_prefix}{task.id}"
        sha = gitutil.commit_to_branch(project_root, branch,
                                       f"{task.title} ({task.id})",
                                       exclude=("harn_env",))
        if not sha:
            return None
        if not gitutil.push_branch(project_root, cfg.git_push_remote, branch):
            return None
        base = cfg.git_pr_base or gitutil.default_branch(project_root) or "main"
        url = prhost.create_pr(project_root, base=base, head=branch,
                               title=f"{task.title} ({task.id})",
                               body=(task.result or task.description or "")[:4000])
        if url:
            task.result = (task.result + "\n\n" if task.result else "") + f"PR: {url}"
            tasks_mod._save(task)
        return url
    except Exception:
        return None
```
Add a tiny `gitutil.default_branch(cwd) -> str` helper (`git symbolic-ref refs/remotes/origin/HEAD` → basename, or `""`), used for the `pr_base` fallback when config leaves it empty.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "push"`
Expected: 3 passed.

- [ ] **Step 5: Full suite** — `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add harn/roles.py harn/roles_runner.py harn/gitutil.py tests/test_push_pr.py
git commit -m "feat(roles): opt-in push→PR stage after a successful role run"
```

---

### Task 5: Studio Bearer-auth gate on sensitive routes

**Files:**
- Modify: `harn/studio.py`
- Test: `tests/test_push_pr.py`

**Interfaces:**
- Consumes: `secrets_store.load(env_dir).get("HARN_API_TOKEN")`.
- Produces: a `_authorized(handler, env_dir, route) -> bool` check applied at the top of `do_POST` (and `do_GET` if any GET route is sensitive) for the protected set `{"/api/agents/run", "/api/agents/generate"}` (`/api/tasks/intake` joins this set in feature A). Returns True if: no token configured (back-compat) OR the request is loopback OR the `Authorization: Bearer <token>` matches (via `hmac.compare_digest`). A protected route with a token configured, a non-loopback client, and a missing/wrong token → the handler responds 401 and does not dispatch.

- [ ] **Step 1: Write the failing tests**

These test the pure auth helper (no socket needed) — the implementer exposes `studio._authorized(...)` taking the pieces it needs, OR a thin `studio._check_bearer(token_configured, client_ip, auth_header)` bool. Prefer a pure function so it's unit-testable without a live server:

```python
def test_bearer_check_no_token_configured_allows_all(tmp_path):
    from harn import studio
    # token unset → always authorized (back-compat)
    assert studio._check_bearer(configured_token="", client_ip="8.8.8.8", auth_header="") is True


def test_bearer_check_loopback_exempt(tmp_path):
    from harn import studio
    assert studio._check_bearer(configured_token="s3cret", client_ip="127.0.0.1", auth_header="") is True
    assert studio._check_bearer(configured_token="s3cret", client_ip="::1", auth_header="") is True


def test_bearer_check_remote_requires_valid_token(tmp_path):
    from harn import studio
    assert studio._check_bearer(configured_token="s3cret", client_ip="8.8.8.8", auth_header="Bearer s3cret") is True
    assert studio._check_bearer(configured_token="s3cret", client_ip="8.8.8.8", auth_header="Bearer wrong") is False
    assert studio._check_bearer(configured_token="s3cret", client_ip="8.8.8.8", auth_header="") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "bearer"`
Expected: FAIL — no `studio._check_bearer`.

- [ ] **Step 3: Implement**

Add to `harn/studio.py` (module level), using `hmac` and `secrets_store`:

```python
import hmac
_PROTECTED_ROUTES = {"/api/agents/run", "/api/agents/generate", "/api/tasks/intake"}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _check_bearer(configured_token: str, client_ip: str, auth_header: str) -> bool:
    """Authorize a sensitive-route request. No token configured → allow
    (back-compat). Loopback client → allow (local operator/UI). Otherwise the
    Authorization header must be `Bearer <token>` matching the configured
    token (constant-time)."""
    if not configured_token:
        return True
    if client_ip in _LOOPBACK:
        return True
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return False
    presented = auth_header[len(prefix):].strip()
    return hmac.compare_digest(presented, configured_token)
```

In `do_POST` (and `do_GET` — protect the same set), right after computing `route` and `env`:
```python
            if route in _PROTECTED_ROUTES:
                from . import secrets_store as _sec
                token = (_sec.load(env).get("HARN_API_TOKEN") or "").strip()
                ip = self.client_address[0] if self.client_address else ""
                if not _check_bearer(token, ip, self.headers.get("Authorization", "")):
                    self._json({"error": "unauthorized"}, 401); return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "bearer"`
Expected: 3 passed.

- [ ] **Step 5: Full suite** — `python3 -m pytest -q` (confirm existing studio tests, which hit routes via the payload functions not the socket, are unaffected — the auth gate is in the handler, not the payloads).

- [ ] **Step 6: Commit**

```bash
git add harn/studio.py tests/test_push_pr.py
git commit -m "feat(studio): Bearer-token gate on agent routes (loopback-exempt, back-compat)"
```

---

### Task 6: Agent API documentation

**Files:**
- Create: `docs/agent-api.md`
- Modify: `README.md`
- Test: `tests/test_push_pr.py`

**Interfaces:** none (docs). A test asserts the doc exists and mentions each endpoint + the auth model.

- [ ] **Step 1: Write the failing test**

```python
def test_agent_api_doc_exists_and_covers_endpoints():
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "docs" / "agent-api.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    for route in ("/api/agents/run", "/api/agents/generate", "/api/tasks/intake"):
        assert route in text
    assert "HARN_API_TOKEN" in text and "Bearer" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "agent_api_doc"`
Expected: FAIL — file missing.

- [ ] **Step 3: Implement**

Write `docs/agent-api.md`: overview, the auth model (Bearer `HARN_API_TOKEN` from `harn_env/secrets.env`, loopback exemption, unset = localhost-only), and each endpoint (`POST /api/agents/run`, `POST /api/agents/generate`, `POST /api/tasks/intake` — note intake ships in the document-intake feature) with request/response JSON shapes and a `curl` example including the `Authorization: Bearer` header. Add a short "Agent API" section to `README.md` linking to it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_push_pr.py -v -k "agent_api_doc"`
Expected: 1 passed.

- [ ] **Step 5: Full suite** — `python3 -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add docs/agent-api.md README.md tests/test_push_pr.py
git commit -m "docs: agent API reference (endpoints + Bearer auth model)"
```

---

### Task 7: Version bump + light integration verification

**Files:** `harn/__init__.py`, `pyproject.toml` (+ a manual curl check).

- [ ] **Step 1:** Bump `__version__`/pyproject to the next patch. Commit `chore: bump version`.
- [ ] **Step 2 (manual):** In a scratch git project with `harn setup`, set `HARN_API_TOKEN=test` in `harn_env/secrets.env`, `harn ui`, then:
  - `curl -sS -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:PORT/api/agents/generate -d '{"description":"x"}'` from loopback → NOT 401 (loopback exempt).
  - Simulate a non-loopback request is hard locally; instead assert via the unit tests (Task 5) that a remote IP without a token → 401. Confirm the loopback path in the browser still works (open Studio, generate an agent — no token needed).
- [ ] **Step 3:** Confirm no regression: full suite green.

No commit for verification beyond the version bump; any bug → fix in the relevant task's files and re-run its tests.
