"""Autonomy level config + the Telegram 'Decide for me' (Auto) button."""
from __future__ import annotations

import harn.telegram as tg
from harn.telegram import TelegramHIL, _AUTO_CALLBACK
from harn import loop
from harn.config import Config


def _ok(result):
    return {"ok": True, "result": result}


# --- autonomy config + prompt directive ----------------------------------- #

def test_autonomy_default_and_clamp(tmp_path):
    (tmp_path / "harn.toml").write_text('[harn]\nautonomy = 0.7\n')
    assert Config.load(tmp_path).autonomy == 0.7
    (tmp_path / "harn.toml").write_text('[harn]\nautonomy = 5\n')
    assert Config.load(tmp_path).autonomy == 1.0          # clamped
    (tmp_path / "harn.toml").write_text('[harn]\nautonomy = -2\n')
    assert Config.load(tmp_path).autonomy == 0.0
    (tmp_path / "harn.toml").write_text('[harn]\nautonomy = "x"\n')
    assert Config.load(tmp_path).autonomy == 0.7          # bad → default


def test_autonomy_env_override(tmp_path, monkeypatch):
    (tmp_path / "harn.toml").write_text('[harn]\nautonomy = 0.2\n')
    monkeypatch.setenv("HARN_AUTONOMY", "0.9")
    assert Config.load(tmp_path).autonomy == 0.9


def test_autonomy_note_varies_by_level():
    low = loop._autonomy_note(0.1)
    mid = loop._autonomy_note(0.7)
    high = loop._autonomy_note(0.95)
    assert "10%" in low and "METICULOUS" in low
    assert "70%" in mid and "BALANCED" in mid
    assert "95%" in high and "DECISIVE" in high


def test_autonomy_note_injected_into_prompt(tmp_path):
    from tests.conftest import make_task
    env = tmp_path / "harn_env"; env.mkdir()
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\nautonomy = 0.2\n')
    t = make_task(env, "PRJ-001")
    cfg = Config.load(env)
    prompt = loop._build_prompt(env, cfg, t)
    assert "Autonomy: 20% self-directed" in prompt
    assert "METICULOUS" in prompt


# --- Telegram Auto button ------------------------------------------------- #

def _cb(update_id, chat_id, data):
    return {"update_id": update_id,
            "callback_query": {"id": "cbid", "data": data,
                               "message": {"chat": {"id": chat_id}}}}


def test_auto_button_press_returns_auto(monkeypatch, tmp_path):
    seq = iter([
        _ok([]),                       # drain
        _ok({"message_id": 100}),      # send question (with button)
        _ok([_cb(7, 42, _AUTO_CALLBACK)]),  # human taps "Decide for me"
        _ok({}),                       # answerCallbackQuery
        _ok({}),                       # edit_message
    ])
    monkeypatch.setattr(tg, "_http_post_json", lambda *a, **k: next(seq))
    hil = TelegramHIL(token="t", chat_id="42")
    reply, source = hil.await_answer("which db?", state_dir=tmp_path,
                                     poll_timeout=0, local_check=lambda: False)
    assert (reply, source) == (None, "auto")


def test_send_includes_auto_button(monkeypatch):
    captured = {}
    def fake_post(url, params, timeout):
        captured.update(params)
        return _ok({"message_id": 1})
    monkeypatch.setattr(tg, "_http_post_json", fake_post)
    hil = TelegramHIL(token="t", chat_id="42")
    hil.send("q", auto_button=True)
    assert "reply_markup" in captured
    btn = captured["reply_markup"]["inline_keyboard"][0][0]
    assert btn["callback_data"] == _AUTO_CALLBACK


def test_handle_block_delegates_on_auto(monkeypatch, tmp_path):
    """When await_answer returns 'auto', the block is recorded as delegated."""
    from harn import state, tasks
    from tests.conftest import make_task
    env = tmp_path / "harn_env"; env.mkdir()
    (env / "harn.toml").write_text('[harn]\nagent = "fake"\n')
    (env / "state").mkdir()
    state.blocked_marker(env / "state").write_text("which db?")
    t = make_task(env, "PRJ-001")
    cfg = Config.load(env)
    st = state.State.load(env / "state")

    monkeypatch.setattr(loop, "_await_answer", lambda *a: (None, "auto"))
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    result = loop._handle_block(env, cfg, st, env / "state", t)

    assert result == "resumed"
    assert state.read_block_question(env / "state") is None  # cleared
    answers = (env / "state" / "ANSWERS.md").read_text()
    assert "Decide for me" in answers


def test_low_autonomy_get_next_task_writes_block(tmp_path, monkeypatch):
    """get_next_task at autonomy ≤ 30% must write BLOCKED.md so harn watch can
    escalate to Telegram — MCP enforces confirmation regardless of Auto Mode."""
    import asyncio
    from harn import scaffold, tasks, state, ENV_DIRNAME
    import harn.mcp_server as ms

    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\nautonomy = 0.2\n'
        '[notify]\nwait_for_reply = false\n'
    )
    tasks.create_task(env, "Add login", task_id="PRJ-001",
                      description="## What\nx\n## Done when\n- works")

    monkeypatch.setenv("HARN_ENV_DIR", str(env))
    monkeypatch.setattr(ms, "_ensure_watch_running", lambda e: None)
    srv = ms.build_server()
    result = str(asyncio.new_event_loop().run_until_complete(
        srv.call_tool("get_next_task", {})))

    # MCP must have written BLOCKED.md
    state_dir = env / "state"
    q = state.read_block_question(state_dir)
    assert q is not None, "BLOCKED.md should be written when autonomy ≤ 30%"
    assert "PRJ-001" in q
    # Response must tell the agent to show the question and not proceed
    assert "CONFIRMATION REQUIRED" in result
    assert "AskUserQuestion" in result
    assert "answer_question" in result
