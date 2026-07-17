"""Spec C: per-step new_session compaction.

`tasks.compact_context` owns the file mechanics (find the not-yet-compacted
raw span of `## Context`, summarize it via a caller-supplied callback,
rewrite the file in place) — incremental across repeated calls, so a second
compaction only touches what's NEW since the first. `loop._compact_step_context`
is the orchestration: resolves the step's own adapter/model, dispatches the
summarization turn, and (only when `use_task_context` is also set) returns
text to inject into the step's own prompt. Both are best-effort — a failed
summarization call must never block the step it guards.
"""
from __future__ import annotations

from harn import loop, tasks, scaffold, ENV_DIRNAME
from harn.adapters.base import AgentResult
from harn.config import Config
from .conftest import make_task


def _step(**kw):
    base = {"kind": "step", "title": "Implement", "body": "One focused change.",
            "id": "step-a", "agent": "", "model": "", "effort": "",
            "temperature": "", "required": [], "skills_recommended": [],
            "tools": [], "tools_recommended": [], "enabled": True,
            "new_session": False, "use_task_context": True}
    base.update(kw)
    return base


class StubAdapter:
    name = "fake"

    def __init__(self, summarize_text="a concise summary", raise_on_summarize=False):
        self.summarize_text = summarize_text
        self.raise_on_summarize = raise_on_summarize
        self.calls = []

    def available(self):
        return True

    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append(prompt)
        if "## Context compaction" in prompt:
            if self.raise_on_summarize:
                raise RuntimeError("summarization boom")
            return AgentResult(ok=True, text=self.summarize_text)
        return AgentResult(ok=True, text="step ran")


# --------------------------------------------------------------------------- #
# tasks.compact_context — pure file-mechanics unit tests
# --------------------------------------------------------------------------- #
def _seed_context(env, task_id, entries):
    for step_id, text in entries:
        tasks.append_context(env, task_id, step_id=step_id, text=text)


def test_compact_context_first_pass_covers_everything_since_the_start(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    _seed_context(env, t.id, [("step-a", "raw output from step a"),
                             ("step-b", "raw output from step b")])

    seen = {}

    def summarize(raw_text, step_ids):
        seen["raw_text"] = raw_text
        seen["step_ids"] = step_ids
        return "SUMMARY-1"

    entry = tasks.compact_context(env, t.id, summarize=summarize)
    assert entry is not None
    assert "SUMMARY-1" in entry
    assert "[compacted @ " in entry
    assert seen["step_ids"] == ["step-a", "step-b"]
    assert "raw output from step a" in seen["raw_text"]
    assert "raw output from step b" in seen["raw_text"]

    # The rewritten file now shows the compacted entry instead of the raw one.
    fresh = tasks.find(env, t.id)
    assert "SUMMARY-1" in fresh.context
    assert "raw output from step a" not in fresh.context
    assert "raw output from step b" not in fresh.context


def test_compact_context_second_pass_only_covers_the_new_span(tmp_path):
    """Earlier compacted spans must be left untouched — a second compaction
    only summarizes what was appended AFTER the first compaction marker."""
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    _seed_context(env, t.id, [("step-a", "first raw span")])
    tasks.compact_context(env, t.id, summarize=lambda raw, ids: "SUMMARY-1")

    # New raw context appended after the first compaction.
    _seed_context(env, t.id, [("step-b", "second raw span")])

    seen = {}

    def summarize(raw_text, step_ids):
        seen["raw_text"] = raw_text
        seen["step_ids"] = step_ids
        return "SUMMARY-2"

    entry = tasks.compact_context(env, t.id, summarize=summarize)
    assert entry is not None
    # Only the NEW span (step-b) was handed to the summarizer — the first
    # compacted entry's own text must not be re-summarized.
    assert seen["step_ids"] == ["step-b"]
    assert "second raw span" in seen["raw_text"]
    assert "SUMMARY-1" not in seen["raw_text"]
    assert "first raw span" not in seen["raw_text"]

    fresh = tasks.find(env, t.id)
    # Both compacted entries now exist in the file; the raw step-b span is gone.
    assert "SUMMARY-1" in fresh.context
    assert "SUMMARY-2" in fresh.context
    assert "second raw span" not in fresh.context


def test_compact_context_returns_none_when_nothing_new_to_compact(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    # No context captured at all yet.
    assert tasks.compact_context(env, t.id, summarize=lambda r, i: "x") is None

    _seed_context(env, t.id, [("step-a", "raw")])
    tasks.compact_context(env, t.id, summarize=lambda r, i: "SUMMARY-1")
    # Immediately compacting again (no new raw entries since) is a no-op.
    calls = []
    result = tasks.compact_context(
        env, t.id, summarize=lambda r, i: calls.append(1) or "SUMMARY-2")
    assert result is None
    assert calls == []


# --------------------------------------------------------------------------- #
# loop._compact_step_context — orchestration + prompt injection
# --------------------------------------------------------------------------- #
def _env_with_task(tmp_path):
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*.json"):
        p.unlink()
    t = make_task(env, "PRJ-001", title="Feat")
    tasks.append_context(env, t.id, step_id="step-prev", text="earlier raw work")
    return env, t


def test_new_session_false_never_touches_context(tmp_path):
    env, t = _env_with_task(tmp_path)
    adapter = StubAdapter()
    cfg = Config.load(env)
    injection = loop._compact_step_context(env, cfg, t, _step(new_session=False), adapter)
    assert injection == ""
    assert adapter.calls == []
    fresh = tasks.find(env, t.id)
    assert "earlier raw work" in fresh.context   # untouched, no rewrite


def test_new_session_true_use_task_context_true_injects_summary(tmp_path):
    env, t = _env_with_task(tmp_path)
    adapter = StubAdapter(summarize_text="the digest")
    cfg = Config.load(env)
    step = _step(new_session=True, use_task_context=True)
    injection = loop._compact_step_context(env, cfg, t, step, adapter)
    assert "## Context from earlier in this task" in injection
    assert "the digest" in injection
    fresh = tasks.find(env, t.id)
    assert "the digest" in fresh.context
    assert "earlier raw work" not in fresh.context


def test_new_session_true_use_task_context_false_compacts_but_does_not_inject(tmp_path):
    env, t = _env_with_task(tmp_path)
    adapter = StubAdapter(summarize_text="the digest")
    cfg = Config.load(env)
    step = _step(new_session=True, use_task_context=False)
    injection = loop._compact_step_context(env, cfg, t, step, adapter)
    assert injection == ""
    # It still compacted and saved — the file was rewritten even though this
    # step's own prompt doesn't get the summary injected.
    fresh = tasks.find(env, t.id)
    assert "the digest" in fresh.context
    assert "earlier raw work" not in fresh.context


def test_failed_compaction_call_does_not_block_or_raise(tmp_path):
    """Best-effort, matching the oracle/reconcile 'never crash the turn'
    convention: if the summarization LLM call raises, the step must still be
    able to run (just without the compacted context)."""
    env, t = _env_with_task(tmp_path)
    adapter = StubAdapter(raise_on_summarize=True)
    cfg = Config.load(env)
    step = _step(new_session=True, use_task_context=True)
    injection = loop._compact_step_context(env, cfg, t, step, adapter)
    assert injection == ""
    # File is untouched — a failed compaction doesn't corrupt the context.
    fresh = tasks.find(env, t.id)
    assert "earlier raw work" in fresh.context
    # The adapter itself remains usable for the step's REAL turn afterward.
    result = adapter.run_turn("do the actual step work", tmp_path)
    assert result.ok and result.text == "step ran"


def test_build_step_prompt_includes_context_injection_section(tmp_path):
    env, t = _env_with_task(tmp_path)
    cfg = Config.load(env)
    prompt = loop._build_step_prompt(
        env, cfg, t, _step(), context_injection="## Context from earlier in this task\nthe digest")
    assert "## Context from earlier in this task" in prompt
    assert "the digest" in prompt


def test_build_step_prompt_omits_context_section_when_empty(tmp_path):
    env, t = _env_with_task(tmp_path)
    cfg = Config.load(env)
    prompt = loop._build_step_prompt(env, cfg, t, _step(), context_injection="")
    assert "## Context from earlier in this task" not in prompt
