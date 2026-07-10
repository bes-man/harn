"""Parallel workflow steps (Phase 3): consecutive same-`parallel`-group steps
run concurrently, each isolated in its own git worktree off a shared
checkpoint, with every agent connector replicated in so ANY provider's CLI
finds the harn MCP server and writes into the ONE shared task context."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harn import loop, tasks, workflows, scaffold, gitutil, ENV_DIRNAME
from harn.adapters.base import AgentResult
from .conftest import make_task


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _step(title, **kw):
    base = {"kind": "step", "title": title, "body": "", "id": "", "agent": "",
            "model": "", "effort": "", "temperature": "", "type": "",
            "command": "", "on_fail": "", "parallel": "", "required": [],
            "tools": [], "enabled": True}
    base.update(kw)
    return base


def _project(tmp_path, nodes):
    _git(["init", "-q"], tmp_path); _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    scaffold.setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    for p in (env / "tasks").glob("*"):
        p.unlink()
    (env / "harn.toml").write_text(
        '[harn]\nagent = "fake"\n[feedback]\ntest_cmd = ""\nrequire_tests = false\n'
        "[loop]\nmax_iterations = 20\n[notify]\nwait_for_reply = false\n")
    _git(["add", "-A"], tmp_path); _git(["commit", "-qm", "base"], tmp_path)
    t = make_task(env, "PRJ-001", title="Feat")
    workflows.save_task_plan(env, t.id, {"preamble": "", "nodes": nodes})
    return env, t


class WritingAdapter:
    """Writes a DISTINCT file per call (keyed by cwd's basename) so two
    concurrent steps never touch the same file — proves real isolation."""
    name = "fake"
    def __init__(self):
        self.calls = []
    def available(self):
        return True
    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        self.calls.append({"prompt": prompt, "cwd": str(cwd)})
        out_file = Path(cwd) / f"output_{Path(cwd).name}.txt"
        out_file.write_text(f"done in {Path(cwd).name}\n")
        return AgentResult(ok=True, text="did the work")


def test_collect_wave_groups_consecutive_same_parallel_id():
    steps = [_step("A", id="s1"), _step("B", id="s2", parallel="wave-1"),
             _step("C", id="s3", parallel="wave-1"), _step("D", id="s4")]
    assert [s["id"] for s in loop._collect_wave(steps, steps[0])] == ["s1"]
    assert [s["id"] for s in loop._collect_wave(steps, steps[1])] == ["s2", "s3"]
    assert [s["id"] for s in loop._collect_wave(steps, steps[3])] == ["s4"]


def test_collect_wave_does_not_merge_non_consecutive_same_group():
    steps = [_step("A", id="s1", parallel="wave-1"),
             _step("B", id="s2"),   # different step in between
             _step("C", id="s3", parallel="wave-1")]
    assert [s["id"] for s in loop._collect_wave(steps, steps[0])] == ["s1"]
    assert [s["id"] for s in loop._collect_wave(steps, steps[2])] == ["s3"]


def test_two_parallel_steps_run_concurrently_and_both_merge(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 2
    cwds = {c["cwd"] for c in fake.calls}
    assert len(cwds) == 2  # each ran in its OWN worktree, never the same dir
    assert not any(c["cwd"] == str(tmp_path) for c in fake.calls)  # never the main tree

    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-be"]["status"] == "ok"
    assert fresh.step_results["s-fe"]["status"] == "ok"


def test_connectors_replicated_into_each_worktree_with_absolute_env_dir(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    (tmp_path / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"harn": {"command": "python3", "args": ["-m", "harn", "mcp"],
                                 "env": {"HARN_ENV_DIR": "harn_env"}}}}))
    # Assert INSIDE the adapter call, while the worktree still exists — this is
    # the moment an agent CLI actually needs the connector file. The worktree
    # is torn down once the wave finishes (see
    # test_no_worktrees_or_refs_leaked_after_a_wave_runs), so checking after
    # `loop.run()` returns would race the cleanup rather than test anything.
    checked = []
    class CapturingAdapter(WritingAdapter):
        def run_turn(self, prompt, cwd, **kw):
            mcp_json = json.loads((Path(cwd) / ".mcp.json").read_text())
            checked.append(
                mcp_json["mcpServers"]["harn"]["env"]["HARN_ENV_DIR"])
            return super().run_turn(prompt, cwd, **kw)
    fake = CapturingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(checked) == 2
    for env_dir_seen in checked:
        assert env_dir_seen == str(env.resolve())


def test_wave_of_one_runs_the_normal_single_step_path(tmp_path, monkeypatch):
    """A `parallel` id with no consecutive sibling must NOT create a worktree
    — it's just a regular step, unchanged from Phase 1/2 behavior."""
    env, t = _project(tmp_path, [
        _step("Solo", id="s1", parallel="wave-lonely"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 1
    assert fake.calls[0]["cwd"] == str(tmp_path)  # ran in the MAIN tree, no worktree


def test_command_type_step_can_participate_in_a_wave(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Lint", id="s-lint", type="command", command="true", parallel="wave-1"),
        _step("Build", id="s-build", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-lint"]["status"] == "ok"
    assert fresh.step_results["s-build"]["status"] == "ok"
    assert len(fake.calls) == 1  # only the agent step called the adapter


def test_wave_can_mix_two_different_agent_providers(tmp_path, monkeypatch):
    """The core agent-agnostic guarantee: nothing in the wave/worktree/merge
    code branches on WHICH provider a step uses. Two steps, two distinct
    (faked) adapters registered under different names, same wave."""
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1", agent="agent-a"),
        _step("Frontend", id="s-fe", parallel="wave-1", agent="agent-b"),
    ])
    agent_a, agent_b = WritingAdapter(), WritingAdapter()
    agent_a.name, agent_b.name = "agent-a", "agent-b"
    # "fake" (the project's [harn] default agent, from _project's harn.toml)
    # must also resolve, since _pick_adapter(cfg) computes a default adapter
    # up front even though both steps here override it explicitly.
    registry = {"agent-a": agent_a, "agent-b": agent_b, "fake": agent_a}
    monkeypatch.setattr(loop, "get_adapter", lambda n: registry[n])
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(agent_a.calls) == 1 and len(agent_b.calls) == 1
    assert (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()
    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-be"]["status"] == "ok"
    assert fresh.step_results["s-fe"]["status"] == "ok"


def test_no_worktrees_or_refs_leaked_after_a_wave_runs(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    code, out, _ = gitutil._run(["worktree", "list", "--porcelain"], tmp_path)
    # only the main worktree should remain
    assert out.count("worktree ") == 1


class CrashingAdapter(WritingAdapter):
    """One wave member's `run_turn` raises (simulating an adapter crash) —
    the CRITICAL regression this test pins down: `_run_parallel_wave` must
    neither leak worktree admin metadata nor let the exception propagate out
    of `loop.run()`."""
    def __init__(self, crash_on: str):
        super().__init__()
        self.crash_on = crash_on

    def run_turn(self, prompt, cwd, timeout=1800, *, model=None, effort=None,
                temperature=None):
        if Path(cwd).name == self.crash_on:
            raise RuntimeError(f"adapter blew up in {self.crash_on}")
        return super().run_turn(prompt, cwd, timeout=timeout, model=model,
                                effort=effort, temperature=temperature)


def test_disjoint_file_patches_both_merge_without_conflict(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()  # writes output_<dirname>.txt — disjoint by construction
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()


def test_conflicting_patches_dispatch_one_merge_agent_turn(tmp_path, monkeypatch):
    (tmp_path / "shared.txt").write_text("original\n")
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    class ConflictingAdapter:
        name = "fake"
        def __init__(self): self.calls = []
        def available(self): return True
        def run_turn(self, prompt, cwd, **kw):
            self.calls.append({"prompt": prompt, "cwd": str(cwd)})
            n = len(self.calls)
            if n <= 2:
                # both parallel steps edit the SAME line incompatibly
                Path(cwd, "shared.txt").write_text(f"edited by call {n}\n")
                return AgentResult(ok=True, text="done")
            # third call = the merge agent turn
            Path(cwd, "shared.txt").write_text("merged resolution\n")
            return AgentResult(ok=True, text="resolved the conflict")
    fake = ConflictingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    assert len(fake.calls) == 3  # 2 parallel + 1 merge turn
    assert (tmp_path / "shared.txt").read_text() == "merged resolution\n"


def test_parallel_step_block_defers_to_sequential_rerun(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    state_dir = env / "state"
    class BlockingAdapter:
        name = "fake"
        def __init__(self): self.calls = 0
        def available(self): return True
        def run_turn(self, prompt, cwd, **kw):
            self.calls += 1
            if "Backend" in prompt:
                (state_dir).mkdir(parents=True, exist_ok=True)
                (state_dir / "BLOCKED.md").write_text("## Question\nWhich DB?\n")
            else:
                Path(cwd, "frontend_done.txt").write_text("ok\n")
            return AgentResult(ok=True, text="...")
    fake = BlockingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    phase = loop.run(tmp_path, env)

    from harn import state as state_mod
    assert phase == state_mod.BLOCKED
    # Frontend's patch merged despite Backend blocking
    assert (tmp_path / "frontend_done.txt").exists()
    fresh = tasks.find(env, t.id)
    assert fresh.step_results.get("s-fe", {}).get("status") == "ok"


def test_merge_never_creates_a_git_commit_even_on_conflict(tmp_path, monkeypatch):
    """harn's standing invariant: it never commits on the user's behalf.
    Applying wave patches, and dispatching a merge-agent turn to resolve a
    conflict, must both leave the merged result as an uncommitted diff."""
    (tmp_path / "shared.txt").write_text("original\n")
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    log_before = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                                capture_output=True, text=True).stdout

    class ConflictingAdapter:
        name = "fake"
        def __init__(self): self.calls = []
        def available(self): return True
        def run_turn(self, prompt, cwd, **kw):
            self.calls.append(cwd)
            n = len(self.calls)
            if n <= 2:
                Path(cwd, "shared.txt").write_text(f"edited by call {n}\n")
                return AgentResult(ok=True, text="done")
            Path(cwd, "shared.txt").write_text("merged resolution\n")
            return AgentResult(ok=True, text="resolved")
    fake = ConflictingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)

    log_after = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                               capture_output=True, text=True).stdout
    assert log_after == log_before  # no new commit object anywhere in this flow
    # the merged result is a real, uncommitted diff sitting in the tree
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                            capture_output=True, text=True).stdout
    assert "shared.txt" in status
    assert (tmp_path / "shared.txt").read_text() == "merged resolution\n"


def test_rollback_one_parallel_step_leaves_sibling_untouched(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()

    r = loop.rollback_parallel_step(tmp_path, env, t.id, "s-be")
    assert r["ok"] is True and r["mode"] == "single-step"
    assert not (tmp_path / "output_s-be.txt").exists()
    assert (tmp_path / "output_s-fe.txt").exists()  # sibling untouched


def test_rollback_falls_back_to_whole_wave_when_patch_no_longer_reverses(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    # someone edits the SAME lines the step's patch touches, after the merge
    (tmp_path / "output_s-be.txt").write_text("edited again after merge, incompatible\n")

    r = loop.rollback_parallel_step(tmp_path, env, t.id, "s-be")
    assert r["mode"] == "whole-wave-fallback"
    assert "note" in r and r["note"]


def test_rollback_unknown_task_reports_error(tmp_path):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
    ])
    r = loop.rollback_parallel_step(tmp_path, env, "NOPE", "s-be")
    assert r["ok"] is False


def test_run_step_rerun_of_parallel_step_does_not_use_shared_wave_checkpoint(tmp_path, monkeypatch):
    """A parallel step's `stage_checkpoints` entry is the WAVE's shared base
    (see `_run_parallel_wave`) — `run_step`'s normal rerun-time restore
    (rollback to `stage_checkpoints[step_id]`) must NOT fire for a parallel
    step, or it would wipe out every sibling's already-merged edit too.
    Independent rollback for a parallel step is `rollback_parallel_step`'s
    job (studio calls it BEFORE requesting this rerun), not `run_step`'s own
    checkpoint restore.
    """
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = WritingAdapter()
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])
    loop.run(tmp_path, env)
    assert (tmp_path / "output_s-fe.txt").exists()

    r = loop.run_step(tmp_path, env, t.id, "s-be", rerun=True)
    assert r["ok"] is True
    # sibling's merged output must survive a parallel step's own rerun
    assert (tmp_path / "output_s-fe.txt").exists()


def test_run_does_not_crash_when_a_wave_member_turn_raises(tmp_path, monkeypatch):
    env, t = _project(tmp_path, [
        _step("Backend", id="s-be", parallel="wave-1"),
        _step("Frontend", id="s-fe", parallel="wave-1"),
    ])
    fake = CrashingAdapter(crash_on="s-fe")
    monkeypatch.setattr(loop, "get_adapter", lambda n: fake)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: [])

    # Must not raise — a crashing adapter degrades to a failed step instead
    # of propagating out of run() (matches the "never let a turn crash the
    # dispatcher" convention used elsewhere in loop.py, e.g. oracle_review).
    phase = loop.run(tmp_path, env)
    assert phase is not None

    fresh = tasks.find(env, t.id)
    assert fresh.step_results["s-be"]["status"] == "ok"
    assert fresh.step_results["s-fe"]["status"] == "failed"

    # No leaked worktree admin metadata (the Critical defect this test pins
    # down): only the main worktree should remain, and none flagged prunable.
    code, out, _ = gitutil._run(["worktree", "list", "--porcelain"], tmp_path)
    assert out.count("worktree ") == 1
    assert "prunable" not in out
