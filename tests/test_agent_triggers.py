"""Agent triggers spec (docs/superpowers/specs/2026-07-18-agent-triggers-design.md):
command parsing, dispatch (id vs new-task-text vs empty), the API route
sharing that same code path, auto-scan respecting claims/active-run/attempt
caps, and Telegram command polling honoring the chat_id trust boundary."""
from __future__ import annotations

import harn.telegram as tg
from harn import loop, roles, roles_runner, scaffold, studio, tasks, triggers, \
    ENV_DIRNAME
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
