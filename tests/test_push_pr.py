"""Push→PR + authenticated API spec (docs/superpowers/specs/2026-07-19-push-pr-and-authenticated-api-design.md)."""
from __future__ import annotations
import subprocess
from harn.config import Config
from harn import ENV_DIRNAME
from harn import gitutil


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
    # HEAD is restored to the original branch; the commit lives on harn/PRJ-1.
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=repo, capture_output=True, text=True).stdout.strip()
    assert branch != "harn/PRJ-1"
    log = subprocess.run(["git", "log", "harn/PRJ-1", "--oneline", "-1"], cwd=repo,
                        capture_output=True, text=True).stdout
    assert "work on PRJ-1" in log


def test_commit_to_branch_nothing_to_commit_returns_none(tmp_path):
    repo = _repo(tmp_path)
    assert gitutil.commit_to_branch(repo, "harn/PRJ-2", "noop") is None


def test_commit_to_branch_non_repo_returns_none(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert gitutil.commit_to_branch(d, "b", "m") is None


def test_commit_to_branch_nothing_to_commit_leaves_original_branch(tmp_path):
    repo = _repo(tmp_path)
    orig = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout.strip()
    assert gitutil.commit_to_branch(repo, "harn/PRJ-X", "noop") is None
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=repo, capture_output=True, text=True).stdout.strip()
    assert branch == orig
    assert subprocess.run(["git", "rev-parse", "--verify", "harn/PRJ-X"],
                          cwd=repo, capture_output=True).returncode != 0


def test_commit_to_branch_success_restores_original_branch(tmp_path):
    repo = _repo(tmp_path)
    orig = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                          cwd=repo, capture_output=True, text=True).stdout.strip()
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    sha = gitutil.commit_to_branch(repo, "harn/PRJ-X", "work on PRJ-X")
    assert sha
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=repo, capture_output=True, text=True).stdout.strip()
    assert branch == orig
    assert subprocess.run(["git", "rev-parse", "--verify", "harn/PRJ-X"],
                          cwd=repo, capture_output=True).returncode == 0
    log = subprocess.run(["git", "log", "harn/PRJ-X", "--oneline"], cwd=repo,
                        capture_output=True, text=True).stdout
    assert "work on PRJ-X" in log


def test_push_branch_non_repo_returns_false(tmp_path):
    d = tmp_path / "plain2"
    d.mkdir()
    assert gitutil.push_branch(d, "origin", "b") is False
