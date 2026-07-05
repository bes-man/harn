"""Token-lean guidance: lean core, on-demand topics, mode toggle, overhead budget."""
from __future__ import annotations

import json
from pathlib import Path

from harn import guidance, scaffold, ENV_DIRNAME
from harn.config import Config


def _setup(tmp_path) -> Path:
    scaffold.setup(tmp_path)
    return tmp_path


# --- lean AGENTS.md ---------------------------------------------------------

def test_lean_agents_under_budget_and_has_hard_rules(tmp_path):
    _setup(tmp_path)
    text = (tmp_path / "AGENTS.md").read_text()
    assert len(text) < 8800, f"lean AGENTS.md too big: {len(text)} chars"  # ~2.2k tok
    for kw in ("ToolSearch", "create_task", "plan mode", "ask_user",
               "run_tests", "read_guidance"):
        assert kw in text, f"missing hard-rule keyword: {kw}"


def test_guidance_index_lists_topics(tmp_path):
    _setup(tmp_path)
    idx = guidance.index(tmp_path / ENV_DIRNAME)
    for topic in ("services", "code-search", "hil", "parallel", "design",
                  "browser", "onboarding", "tasks"):
        assert topic in idx


def test_read_guidance_returns_body_and_errors_cleanly(tmp_path):
    _setup(tmp_path)
    env = tmp_path / ENV_DIRNAME
    body = guidance.read(env, "parallel")
    assert body and "depends_on" in body
    assert guidance.read(env, "nonsense") is None


# --- full mode --------------------------------------------------------------

def test_full_mode_produces_verbose_agents(tmp_path):
    scaffold.setup(tmp_path)
    toml = tmp_path / ENV_DIRNAME / "harn.toml"
    toml.write_text(toml.read_text().replace('guidance = "lean"', 'guidance = "full"'))
    scaffold.refresh_agents_md(tmp_path)
    text = (tmp_path / "AGENTS.md").read_text()
    assert len(text) > 12000  # the verbose variant is back
    assert Config.load(tmp_path / ENV_DIRNAME).guidance == "full"


# --- overhead budget --------------------------------------------------------

def test_fixed_overhead_under_target(tmp_path):
    """AGENTS.md + CLAUDE.md + tool docstrings should be well under the old
    ~8.9k tokens. Budget 4800: workflow is now always-on (a WORKFLOW.md pointer
    for every agent + read_workflow/save_workflow), tasks can run under named
    workflow presets (set_task_workflow + read_workflow's preset footer), and
    tasks carry file attachments (save_attachment/list_attachments/
    read_attachment). WORKFLOW.md itself is read-on-demand, so it costs 0
    context per turn; the studio Tools tab's longer human-facing notes
    (tool_notes.py) are UI-only and never touch this budget."""
    import inspect, re
    from harn import mcp_server
    _setup(tmp_path)
    agents = len((tmp_path / "AGENTS.md").read_text())
    claude = len((tmp_path / "CLAUDE.md").read_text())
    src = inspect.getsource(mcp_server.build_server)
    docs = sum(len(d) for d in re.findall(r'"""(.*?)"""', src, re.DOTALL))
    total_tok = (agents + claude + docs) // 4
    assert total_tok <= 4800, f"fixed overhead {total_tok} tok exceeds budget"
