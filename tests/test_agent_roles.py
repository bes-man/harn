"""Agent roles spec (docs/superpowers/specs/2026-07-18-agent-roles-design.md):
role discovery, secrets fail-fast + name-only prompt injection, running a
role (status transition / oracle-fail keeps status / no next_status = no
transition), and worktree isolation merging its patch back."""
from __future__ import annotations

import subprocess

from harn import gitutil, loop, roles, roles_runner, scaffold, secrets_store, \
    tasks, workflows, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


class RecordingAdapter:
    name = "fake"

    def __init__(self, text="done"):
        self.calls = []
        self.text = text

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt, "model": model, "effort": effort})
        return AgentResult(ok=True, text=self.text)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _role_md(env, name, **fm):
    (env / "agents").mkdir(parents=True, exist_ok=True)
    lines = ["---", f"name: {name}"]
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(v)}]")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            lines.append(f"{k}: {v}")
    lines += ["---", "## Role", f"You are the {name}."]
    (env / "agents" / f"{name}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _project(tmp_path, n_steps=1):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 40\noracle = false\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    plan = {"preamble": "", "nodes": [
        {"kind": "step", "title": f"Step {i}", "body": f"do part {i}",
         "id": f"step-{i:06x}", "agent": "", "model": "", "effort": "",
         "temperature": "", "required": [], "tools": [], "enabled": True}
        for i in range(1, n_steps + 1)]}
    workflows.save_task_plan(env, t.id, plan)
    return env, t


# --- discovery ------------------------------------------------------------ #

def test_discover_finds_valid_role(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    _role_md(env, "analyst", command="analyst", status="analyzing",
             next_status="analyzed", workflow="analyst-flow")
    found = roles.discover(env)
    assert len(found) == 1
    r = found[0]
    assert r.name == "analyst" and r.status == "analyzing"
    assert r.next_status == "analyzed" and r.workflow == "analyst-flow"
    assert r.trigger == "manual" and r.oracle is True and r.isolation == "main"


def test_discover_skips_role_missing_status(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    _role_md(env, "broken", command="broken")   # no status:
    assert roles.discover(env) == []


def test_discover_no_agents_dir_returns_empty(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    assert roles.discover(env) == []


def test_discover_parses_secrets_list_and_booleans(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    _role_md(env, "ops", status="deploying", secrets=["SSH_HOST", "SSH_USER"],
             oracle=False, isolation="worktree")
    r = roles.find(env, "ops")
    assert r.secrets == ["SSH_HOST", "SSH_USER"]
    assert r.oracle is False
    assert r.isolation == "worktree"


def test_find_by_command_or_name(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    _role_md(env, "analyst", command="an", status="analyzing")
    assert roles.find(env, "analyst").name == "analyst"
    assert roles.find(env, "an").name == "analyst"
    assert roles.find(env, "nope") is None


# --- secrets --------------------------------------------------------------- #

def test_secrets_missing_lists_undeclared_names(tmp_path):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    secrets_store.ensure_file(env)
    assert secrets_store.missing(env, ["SSH_HOST"]) == ["SSH_HOST"]
    (env / "secrets.env").write_text("SSH_HOST=1.2.3.4\n", encoding="utf-8")
    assert secrets_store.missing(env, ["SSH_HOST", "SSH_USER"]) == ["SSH_USER"]
    assert secrets_store.missing(env, ["SSH_HOST"]) == []


def test_secrets_injected_reaches_process_env_and_restores_after(tmp_path, monkeypatch):
    env = tmp_path / ENV_DIRNAME
    env.mkdir()
    (env / "secrets.env").write_text("MY_KEY=topsecret\n", encoding="utf-8")
    monkeypatch.delenv("MY_KEY", raising=False)
    with secrets_store.injected(env, ["MY_KEY"]):
        import os
        assert os.environ["MY_KEY"] == "topsecret"
    import os
    assert "MY_KEY" not in os.environ


def test_run_role_refuses_to_start_with_missing_secrets(tmp_path):
    env, t = _project(tmp_path)
    _role_md(env, "ops", status=t.status, secrets=["SSH_HOST"])
    result = roles_runner.run_role(tmp_path, env, t.id, "ops")
    assert result["ok"] is False
    assert "SSH_HOST" in result["error"]


def test_role_secret_names_in_prompt_but_value_absent(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    (env / "secrets.env").write_text("API_TOKEN=sekret-value-xyz\n", encoding="utf-8")
    _role_md(env, "ops", status=t.status, secrets=["API_TOKEN"])
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    result = roles_runner.run_role(tmp_path, env, t.id, "ops")
    assert result["ok"] is True
    prompt = fake.calls[0]["prompt"]
    assert "API_TOKEN" in prompt
    assert "sekret-value-xyz" not in prompt


# --- running a role --------------------------------------------------------- #

def test_run_role_unknown_role(tmp_path):
    env, t = _project(tmp_path)
    result = roles_runner.run_role(tmp_path, env, t.id, "nope")
    assert result["ok"] is False


def test_run_role_unknown_task(tmp_path):
    env, t = _project(tmp_path)
    _role_md(env, "analyst", status="todo")
    result = roles_runner.run_role(tmp_path, env, "NOPE", "analyst")
    assert result["ok"] is False


# --- resuming a role run: skip completed steps, stop when blocked ---------- #

def test_run_role_skips_steps_already_recorded_ok(tmp_path, monkeypatch):
    """A role re-dispatched after an interruption (another Telegram command,
    the Studio Resume button, an answer-triggered auto-resume) must pick up
    where it left off, not re-run every step from the start — that both
    wastes tokens and, observed live, can abort an otherwise-fine resume when
    an ALREADY-SUCCEEDED step happens to fail on re-run (a transient CLI auth
    error), even though the actually-pending step was further along."""
    env, t = _project(tmp_path, n_steps=2)
    _role_md(env, "analyst", status=t.status, oracle=False)
    task = tasks.find(env, t.id)
    task.step_results["step-000001"] = {"status": "ok", "output": "done earlier"}
    tasks._save(task)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")

    assert result["ok"] is True
    assert len(fake.calls) == 1, "only the not-yet-done step should run"
    assert "do part 1" not in fake.calls[0]["prompt"]


def test_run_role_stops_chain_when_a_step_leaves_the_task_blocked(tmp_path, monkeypatch):
    """A step can succeed AS A TURN (the agent called ask_user and ended
    cleanly) while leaving the task genuinely blocked on a human answer.
    Running the next step's turn anyway can't make progress and just burns
    tokens — the chain must stop as soon as the task is blocked."""
    from harn import state as state_mod

    env, t = _project(tmp_path, n_steps=2)
    _role_md(env, "analyst", status=t.status)

    class BlockingThenRecordingAdapter(RecordingAdapter):
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                    temperature=None):
            if not self.calls:
                state_dir = env / "state"
                st = state_mod.State.load(state_dir)
                st.current_task = t.id
                st.block("Which option — A or B?")
                st.save(state_dir)
            return super().run_turn(prompt, cwd, timeout=timeout, model=model,
                                    effort=effort, temperature=temperature)

    fake = BlockingThenRecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")

    assert result["ok"] is False
    assert result.get("blocked") is True
    assert len(fake.calls) == 1, "the second step must not run while blocked"


def test_successful_run_transitions_to_next_status(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    (env / "harn.toml").write_text(
        (env / "harn.toml").read_text(encoding="utf-8") +
        '\n[board]\nstatuses = ["todo", "analyzed", "done"]\n', encoding="utf-8")
    _role_md(env, "analyst", status=t.status, next_status="analyzed", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")
    assert result["ok"] is True
    assert result["status"] == "analyzed"
    assert tasks.find(env, t.id).status == "analyzed"


def test_empty_next_status_means_no_transition(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    _role_md(env, "analyst", status=t.status, oracle=False)   # no next_status
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")
    assert result["ok"] is True
    assert result["status"] == t.status
    assert tasks.find(env, t.id).status == t.status


def test_oracle_fail_keeps_status_and_records_verdict(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    _role_md(env, "analyst", status=t.status, next_status="analyzed", oracle=True)
    step_adapter = RecordingAdapter(text="done")
    oracle_adapter = RecordingAdapter(text="ORACLE: FAIL - not good enough")
    calls = {"n": 0}

    def pick(name):
        calls["n"] += 1
        # loop.run_step calls get_adapter for the step; roles_runner's own
        # oracle check also goes through get_adapter (via _pick_oracle_adapter).
        return oracle_adapter if calls["n"] > 1 else step_adapter

    monkeypatch.setattr(loop, "get_adapter", pick)
    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")
    assert result["ok"] is False
    reloaded = tasks.find(env, t.id)
    assert reloaded.status == t.status   # unchanged — not moved to next_status
    assert any(e.event == "role_oracle_fail" for e in reloaded.review_log)


def test_oracle_pass_transitions_to_next_status(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    (env / "harn.toml").write_text(
        (env / "harn.toml").read_text(encoding="utf-8") +
        '\n[board]\nstatuses = ["todo", "analyzed", "done"]\n', encoding="utf-8")
    _role_md(env, "analyst", status=t.status, next_status="analyzed", oracle=True)
    step_adapter = RecordingAdapter(text="done")
    oracle_adapter = RecordingAdapter(text="ORACLE: PASS")
    calls = {"n": 0}

    def pick(name):
        calls["n"] += 1
        return oracle_adapter if calls["n"] > 1 else step_adapter

    monkeypatch.setattr(loop, "get_adapter", pick)
    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")
    assert result["ok"] is True
    assert tasks.find(env, t.id).status == "analyzed"


def test_manual_run_on_status_mismatch_warns_and_proceeds(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    _role_md(env, "analyst", status="some_other_status", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")
    assert result["ok"] is True
    assert result["warning"] is not None


# --- isolation --------------------------------------------------------------- #

def test_main_isolation_runs_in_place(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    _role_md(env, "analyst", status=t.status, isolation="main", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    before_worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    result = roles_runner.run_role(tmp_path, env, t.id, "analyst")
    assert result["ok"] is True
    after_worktrees = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert before_worktrees == after_worktrees


def test_worktree_isolation_merges_patch_and_cleans_up(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    _role_md(env, "builder", status=t.status, isolation="worktree", oracle=False)

    def make_change(prompt, cwd, timeout=1800, *, model=None, effort=None,
                    temperature=None):
        (cwd / "new_file.txt").write_text("hello from the role\n", encoding="utf-8")
        return AgentResult(ok=True, text="done")

    fake = RecordingAdapter()
    fake.run_turn = make_change
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = roles_runner.run_role(tmp_path, env, t.id, "builder")
    assert result["ok"] is True
    assert (tmp_path / "new_file.txt").exists()
    assert (tmp_path / "new_file.txt").read_text(encoding="utf-8") == "hello from the role\n"

    leftover = subprocess.run(
        ["git", "worktree", "list"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert leftover.count("\n") <= 1   # only the main worktree remains


# --- two-role chain on a custom pipeline ------------------------------------- #

def test_two_role_chain_walks_custom_pipeline_and_writes_result(tmp_path, monkeypatch):
    env, t = _project(tmp_path)
    (env / "harn.toml").write_text(
        (env / "harn.toml").read_text(encoding="utf-8") +
        '\n[board]\nstatuses = ["new", "analyzing", "analyzed", "developing", "done"]\n',
        encoding="utf-8",
    )
    t.status = "analyzing"
    tasks._save(t)
    _role_md(env, "analyst", status="analyzing", next_status="analyzed", oracle=False)
    _role_md(env, "developer", status="analyzed", next_status="done", oracle=False)

    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    r1 = roles_runner.run_role(tmp_path, env, t.id, "analyst")
    assert r1["ok"] is True and r1["status"] == "analyzed"

    r2 = roles_runner.run_role(tmp_path, env, t.id, "developer")
    assert r2["ok"] is True and r2["status"] == "done"

    final = tasks.find(env, t.id)
    assert final.status == "done"
