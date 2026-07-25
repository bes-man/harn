"""Agent triggers spec (docs/superpowers/specs/2026-07-18-agent-triggers-design.md):
command parsing, dispatch (id vs new-task-text vs empty), the API route
sharing that same code path, auto-scan respecting claims/active-run/attempt
caps, and Telegram command polling honoring the chat_id trust boundary."""
from __future__ import annotations

import harn.telegram as tg
from harn import config, loop, roles, roles_runner, scaffold, studio, tasks, \
    triggers, ENV_DIRNAME
from harn.adapters.base import AgentResult
from harn.telegram import TelegramHIL
import subprocess


class RecordingAdapter:
    name = "fake"

    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt})
        return AgentResult(ok=True, text="done")


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
        '[harn]\nagent = "fake"\nproject = "prj"\n[feedback]\ntest_cmd = ""\n'
        "require_tests = false\n[loop]\nmax_iterations = 40\noracle = false\n"
        "[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "base"], tmp_path)
    return env


def _plan(env, task_id, n_steps=1):
    from harn import workflows
    plan = {"preamble": "", "nodes": [
        {"kind": "step", "title": f"Step {i}", "body": f"do part {i}",
         "id": f"step-{i:06x}", "agent": "", "model": "", "effort": "",
         "temperature": "", "required": [], "tools": [], "enabled": True}
        for i in range(1, n_steps + 1)]}
    workflows.save_task_plan(env, task_id, plan)


# --- parse_command ----------------------------------------------------- #

def test_parse_command_extracts_command_and_rest():
    assert triggers.parse_command("/analyst PRJ-001") == ("analyst", "PRJ-001")


def test_parse_command_handles_bot_suffix_and_no_args():
    assert triggers.parse_command("/analyst@my_bot") == ("analyst", "")


def test_parse_command_none_for_plain_text():
    assert triggers.parse_command("just chatting") is None


def test_parse_command_empty_text():
    assert triggers.parse_command("") is None


# --- dispatch_command ---------------------------------------------------- #

def test_dispatch_unknown_command(tmp_path):
    env = _project(tmp_path)
    result = triggers.dispatch_command(tmp_path, env, "nope", "some text")
    assert result["ok"] is False
    assert "unknown command" in result["error"]


def test_dispatch_empty_arg_returns_usage(tmp_path):
    env = _project(tmp_path)
    _role_md(env, "analyst", status="todo")
    result = triggers.dispatch_command(tmp_path, env, "analyst", "")
    assert result["ok"] is False
    assert "usage" in result["error"]


def test_dispatch_with_existing_task_id_launches_that_task(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "Existing task")
    _plan(env, t.id)
    _role_md(env, "analyst", status=t.status, oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    result = triggers.dispatch_command(tmp_path, env, "analyst", t.id)
    assert result["ok"] is True
    assert result["task_id"] == t.id


def test_dispatch_with_unknown_task_id_errors(tmp_path):
    env = _project(tmp_path)
    _role_md(env, "analyst", status="todo")
    result = triggers.dispatch_command(tmp_path, env, "analyst", "PRJ-999")
    assert result["ok"] is False
    assert "PRJ-999" in result["error"]


def test_dispatch_with_free_text_creates_new_task_and_launches(tmp_path, monkeypatch):
    env = _project(tmp_path)
    _role_md(env, "analyst", status="todo", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    real_create = tasks.create_task
    created = {}
    def wrap_create(*a, **kw):
        t = real_create(*a, **kw)
        _plan(env, t.id)
        created["id"] = t.id
        return t
    monkeypatch.setattr(tasks, "create_task", wrap_create)
    monkeypatch.setattr(triggers.tasks_mod, "create_task", wrap_create)

    result = triggers.dispatch_command(tmp_path, env, "analyst",
                                       "Investigate the outage\nmore detail")
    assert result["ok"] is True
    new_task = tasks.find(env, created["id"])
    assert new_task.title == "Investigate the outage"


def test_dispatch_by_role_name_not_just_command(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "T")
    _plan(env, t.id)
    _role_md(env, "analyst", command="an", status=t.status, oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    result = triggers.dispatch_command(tmp_path, env, "analyst", t.id)
    assert result["ok"] is True


# --- run_agent_payload / API route --------------------------------------- #

def test_run_agent_payload_missing_name(tmp_path):
    env = _project(tmp_path)
    result = triggers.run_agent_payload(tmp_path, env, {"task_id": "X"})
    assert result["ok"] is False


def test_run_agent_payload_missing_arg(tmp_path):
    env = _project(tmp_path)
    _role_md(env, "analyst", status="todo")
    result = triggers.run_agent_payload(tmp_path, env, {"name": "analyst"})
    assert result["ok"] is False


def test_run_agent_payload_shares_dispatch_command_code_path(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "T")
    _plan(env, t.id)
    _role_md(env, "analyst", status=t.status, oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    called = {}
    real = triggers.dispatch_command
    def spy(*a, **kw):
        called["hit"] = True
        return real(*a, **kw)
    monkeypatch.setattr(triggers, "dispatch_command", spy)

    result = triggers.run_agent_payload(tmp_path, env, {"name": "analyst", "task_id": t.id})
    assert result["ok"] is True
    assert called.get("hit") is True


def test_studio_api_agents_run_route_calls_run_agent_payload(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "T")
    _plan(env, t.id)
    _role_md(env, "analyst", status=t.status, oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    result = studio.run_agent_payload(env, {"name": "analyst", "task_id": t.id})
    assert result["ok"] is True
    assert result["task_id"] == t.id


# --- auto_scan ------------------------------------------------------------ #

def test_auto_scan_launches_exactly_one_run(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t1 = tasks.create_task(env, "First")
    t2 = tasks.create_task(env, "Second")
    _plan(env, t1.id)
    _plan(env, t2.id)
    _role_md(env, "analyst", status="todo", trigger="auto", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = triggers.auto_scan(tmp_path, env)
    assert result is not None and result["ok"] is True
    assert len(fake.calls) == 1   # only one task launched this tick


def test_auto_scan_ignores_manual_trigger_roles(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "First")
    _plan(env, t.id)
    _role_md(env, "analyst", status="todo", trigger="manual", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = triggers.auto_scan(tmp_path, env)
    assert result is None
    assert fake.calls == []


def test_auto_scan_skips_claimed_tasks(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "First")
    _plan(env, t.id)
    t.claimed_by = "someone"
    tasks._save(t)
    _role_md(env, "analyst", status="todo", trigger="auto", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = triggers.auto_scan(tmp_path, env)
    assert result is None


def test_auto_scan_respects_active_run(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "First")
    _plan(env, t.id)
    _role_md(env, "analyst", status="todo", trigger="auto", oracle=False)
    monkeypatch.setattr(triggers.runner_mod, "active", lambda ed: {"task_id": "OTHER"})
    result = triggers.auto_scan(tmp_path, env)
    assert result is None


def test_auto_scan_skips_tasks_that_hit_attempts_cap(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "First")
    _plan(env, t.id)
    t.step_results["step-000001"] = {"attempts": loop._MAX_STEP_ATTEMPTS}
    tasks._save(t)
    _role_md(env, "analyst", status="todo", trigger="auto", oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = triggers.auto_scan(tmp_path, env)
    assert result is None
    assert fake.calls == []


# --- resume_scan: retry unfinished-but-unblocked work ---------------------- #

def _claimed_unfinished(env, tmp_path, *, role="analyst"):
    """A task mid-workflow: claimed by `role`, step 1 done, step 2 pending."""
    t = tasks.create_task(env, "Half done")
    _plan(env, t.id, n_steps=2)
    t.status = tasks.IN_PROGRESS
    t.claimed_by = role
    t.step_results["step-000001"] = {"status": "ok"}
    tasks._save(t)
    _role_md(env, role, status=tasks.IN_PROGRESS, oracle=False)
    return t


def test_resume_scan_relaunches_an_unfinished_unblocked_task(tmp_path, monkeypatch):
    """The self-healing path for TRANSIENT faults (rate limit, expired CLI
    session, network blip): without it a task sits stopped mid-workflow until
    a human notices — observed live, PRJ-001 sat overnight after its CLI's
    OAuth session expired, with several steps still pending."""
    env = _project(tmp_path)
    t = _claimed_unfinished(env, tmp_path)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = triggers.resume_scan(tmp_path, env)

    assert result is not None and result["ok"] is True
    assert result["task_id"] == t.id
    assert len(fake.calls) == 1, "only the not-yet-done step should run"


def test_resume_scan_skips_a_task_blocked_on_a_human_answer(tmp_path, monkeypatch):
    """A pending question is a BLOCKER, not a transient fault — retrying it
    can't make progress and would just burn tokens on 'still waiting' turns."""
    from harn import state as state_mod
    env = _project(tmp_path)
    t = _claimed_unfinished(env, tmp_path)
    st = state_mod.State(current_task=t.id)
    st.block("Which option — A or B?")
    st.save(env / "state")
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    assert triggers.resume_scan(tmp_path, env) is None
    assert fake.calls == []


def test_resume_scan_skips_fully_finished_and_unclaimed_tasks(tmp_path, monkeypatch):
    env = _project(tmp_path)
    done = tasks.create_task(env, "All done")
    _plan(env, done.id, n_steps=2)
    done.status = tasks.IN_PROGRESS
    done.claimed_by = "analyst"
    done.step_results = {"step-000001": {"status": "ok"}, "step-000002": {"status": "ok"}}
    tasks._save(done)
    unclaimed = tasks.create_task(env, "Nobody's")
    _plan(env, unclaimed.id, n_steps=2)
    _role_md(env, "analyst", status=tasks.IN_PROGRESS, oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    assert triggers.resume_scan(tmp_path, env) is None
    assert fake.calls == []


def test_resume_scan_never_races_an_active_run(tmp_path, monkeypatch):
    env = _project(tmp_path)
    _claimed_unfinished(env, tmp_path)
    monkeypatch.setattr(triggers.runner_mod, "active", lambda ed: {"task_id": "OTHER"})
    assert triggers.resume_scan(tmp_path, env) is None


def test_resume_scan_clears_the_spent_per_step_attempt_cap(tmp_path, monkeypatch):
    """The per-step cap (_MAX_STEP_ATTEMPTS=2) is spent IN FULL by a single
    run, so gating scheduled resumes on it would allow exactly one retry and
    then never again — useless for the case this feature exists for: a rate
    limit or expired session lasting hours. A scheduled resume happens against
    a possibly-changed world, so it clears those counters (same reasoning
    loop.answer() already applies when a human intervenes) and relies on its
    own budget instead."""
    env = _project(tmp_path)
    t = _claimed_unfinished(env, tmp_path)
    t.step_results["step-000002"] = {"status": "failed", "attempts": loop._MAX_STEP_ATTEMPTS}
    tasks._save(t)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    result = triggers.resume_scan(tmp_path, env)

    assert result is not None and result["ok"] is True
    assert len(fake.calls) == 1


def test_resume_scan_gives_up_after_its_own_budget_without_progress(tmp_path, monkeypatch):
    """Bounded so a permanently broken task can't retry forever — but the
    budget RESETS on real progress, so a task that keeps advancing keeps
    earning retries."""
    env = _project(tmp_path)
    t = _claimed_unfinished(env, tmp_path)

    class FailingAdapter(RecordingAdapter):
        """Stands in for the transient fault this feature exists for (rate
        limit / expired session): the step keeps failing, so the task never
        progresses and the budget must eventually stop the retries."""
        def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                    temperature=None):
            self.calls.append({"prompt": prompt})
            return AgentResult(ok=False, text="rate limit")

    monkeypatch.setattr(loop, "get_adapter", lambda n: FailingAdapter())
    cfg = config.Config.load(env)
    cfg.resume_max_attempts = 2

    assert triggers.resume_scan(tmp_path, env, cfg=cfg) is not None
    assert triggers.resume_scan(tmp_path, env, cfg=cfg) is not None
    assert triggers.resume_scan(tmp_path, env, cfg=cfg) is None, "budget spent"

    # Real progress (the stuck step finally completed) refills the budget.
    fresh = tasks.find(env, t.id)
    fresh.step_results["step-000002"] = {"status": "ok"}
    tasks._save(fresh)
    _plan(env, t.id, n_steps=3)
    assert triggers.resume_scan(tmp_path, env, cfg=cfg) is not None


# --- Telegram command polling: trust boundary ----------------------------- #

def _upd(update_id, chat_id, text, is_bot=False):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "from": {"is_bot": is_bot}, "text": text}}


def test_poll_commands_only_from_trusted_chat(monkeypatch, tmp_path):
    batch = {"ok": True, "result": [
        _upd(1, 999, "/analyst PRJ-001"),      # foreign chat — ignored
        _upd(2, 42, "not a command"),          # trusted chat, not a command — ignored
        _upd(3, 42, "/analyst PRJ-002"),       # trusted chat, a command — kept
        _upd(4, 42, "/dev PRJ-003", is_bot=True),  # bot noise — ignored
    ]}
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: batch)
    hil = TelegramHIL(token="t", chat_id="42")
    commands = hil.poll_commands(tmp_path)
    assert [c["text"] for c in commands] == ["/analyst PRJ-002"]


def test_poll_commands_persists_offset(monkeypatch, tmp_path):
    batch = {"ok": True, "result": [_upd(10, 42, "/analyst PRJ-001")]}
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: batch)
    hil = TelegramHIL(token="t", chat_id="42")
    hil.poll_commands(tmp_path)
    assert (tmp_path / tg._OFFSET_FILE).read_text().strip() == "11"


# --- watch() integration: commands get routed and replied to -------------- #

def test_watch_tick_routes_telegram_command_and_replies(tmp_path, monkeypatch):
    env = _project(tmp_path)
    t = tasks.create_task(env, "T")
    _plan(env, t.id)
    _role_md(env, "analyst", status=t.status, oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    sent = []

    class FakeHIL:
        def poll_commands(self, state_dir):
            return [{"text": f"/analyst {t.id}", "message_id": 1}]

        def poll_updates(self, state_dir):
            # watch() now drains via poll_updates (single-drain to avoid
            # double-consuming the Telegram offset alongside documents) —
            # mirror poll_commands's return so this test's command-routing
            # assertion is unaffected.
            return {"commands": self.poll_commands(state_dir), "documents": []}

        def send(self, text, **kw):
            sent.append(text)
            return 1

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda *_: FakeHIL()))
    loop.watch(env, tmp_path, _once=True)
    assert any("✅" in s for s in sent)


def test_watch_tick_sends_immediate_ack_before_result_with_content(tmp_path, monkeypatch):
    """A role run can take minutes — the human should hear "started" right
    away (not just the final ✅/❌ once everything's done), and the final
    reply should carry the task's actual content, not just a status word."""
    env = _project(tmp_path)
    t = tasks.create_task(env, "T", description="Some spec content here")
    _plan(env, t.id)
    _role_md(env, "analyst", status=t.status, oracle=False)
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    sent = []

    class FakeHIL:
        def poll_commands(self, state_dir):
            return [{"text": f"/analyst {t.id}", "message_id": 1}]

        def poll_updates(self, state_dir):
            return {"commands": self.poll_commands(state_dir), "documents": []}

        def send(self, text, **kw):
            sent.append(text)
            return 1

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda *_: FakeHIL()))
    loop.watch(env, tmp_path, _once=True)

    assert len(sent) == 2
    assert sent[0].startswith("🚀 Resuming") and t.id in sent[0]
    assert sent[1].startswith(f"✅ {t.id}")
    assert "Some spec content here" in sent[1]


def test_watch_tick_ack_says_created_for_a_brand_new_task(tmp_path, monkeypatch):
    env = _project(tmp_path)
    _role_md(env, "analyst", status="todo", oracle=False)
    _plan(env, "PRJ-001")  # plan keyed by the id the auto-counter will assign
    fake = RecordingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)

    sent = []

    class FakeHIL:
        def poll_commands(self, state_dir):
            return [{"text": "/analyst Fix the thing", "message_id": 1}]

        def poll_updates(self, state_dir):
            return {"commands": self.poll_commands(state_dir), "documents": []}

        def send(self, text, **kw):
            sent.append(text)
            return 1

    monkeypatch.setattr(loop.TelegramHIL, "from_env", staticmethod(lambda *_: FakeHIL()))
    loop.watch(env, tmp_path, _once=True)

    assert sent[0].startswith("🚀 Created")
