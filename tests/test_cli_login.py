"""`harn login` — harn runs the agent CLI's login flow, the human approves it.

Why this command exists at all: harn shells out to the agent CLI for every
turn, so the CLI needs its OWN login. A signed-in desktop app does NOT cover
it — that session lives inside the app, refreshed in-process and never
written out. Diagnosed live on a machine where Claude Desktop worked
perfectly while every `claude -p` subprocess failed, which reasonably reads
as "harn is broken" rather than "the CLI is logged out".

harn cannot complete the login: it's an interactive OAuth flow needing a real
browser and the account holder's approval (verified — `claude setup-token`
blocks on interactive input with stdin closed). harn picks the right command
and runs it; the human approves; the CLI writes to its own store and harn
never sees a token.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from harn import cli


def _args(tmp_path, agent="claude", force=False):
    return SimpleNamespace(path=str(tmp_path), agent=agent, force=force)


class _Adapter:
    name = "claude"
    def __init__(self, status, login=None):
        self._status, self._login = status, login
        self.checks = 0
    def auth_status(self):
        self.checks += 1
        return self._status.pop(0) if isinstance(self._status, list) else self._status
    def login_command(self):
        return self._login


def test_login_runs_the_agents_own_flow_attached_to_the_terminal(tmp_path, capsys):
    """No capture_output: an interactive OAuth flow must own the tty, or the
    human never sees the prompt and it silently hangs."""
    adapter = _Adapter([("expired", "not signed in"), ("ok", "subscription")],
                       login=["/fake/claude", "setup-token"])
    with patch.object(cli, "get_adapter", create=True), \
         patch("harn.adapters.get_adapter", return_value=adapter), \
         patch("subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0)
        rc = cli.cmd_login(_args(tmp_path))

    assert rc == 0
    assert run.call_args[0][0] == ["/fake/claude", "setup-token"]
    assert "capture_output" not in run.call_args.kwargs
    assert "signed in" in capsys.readouterr().out


def test_login_is_a_noop_when_already_signed_in(tmp_path, capsys):
    adapter = _Adapter(("ok", "subscription"), login=["/fake/claude", "setup-token"])
    with patch("harn.adapters.get_adapter", return_value=adapter), \
         patch("subprocess.run") as run:
        rc = cli.cmd_login(_args(tmp_path))

    assert rc == 0
    run.assert_not_called()
    assert "already signed in" in capsys.readouterr().out


def test_force_re_runs_even_when_signed_in(tmp_path):
    adapter = _Adapter(("ok", "subscription"), login=["/fake/claude", "setup-token"])
    with patch("harn.adapters.get_adapter", return_value=adapter), \
         patch("subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0)
        cli.cmd_login(_args(tmp_path, force=True))

    run.assert_called_once()


def test_login_does_not_claim_success_the_cli_itself_denies(tmp_path, capsys):
    """The flow can exit 0 having been cancelled in the browser. Trust the
    CLI's own verdict afterwards, not the exit code."""
    adapter = _Adapter([("expired", "not signed in"), ("expired", "not signed in")],
                       login=["/fake/claude", "setup-token"])
    with patch("harn.adapters.get_adapter", return_value=adapter), \
         patch("subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0)
        rc = cli.cmd_login(_args(tmp_path))

    assert rc == 1
    assert "still reports" in capsys.readouterr().err


def test_login_says_so_when_harn_knows_no_command_for_that_agent(tmp_path, capsys):
    adapter = _Adapter(("expired", "nope"), login=None)
    with patch("harn.adapters.get_adapter", return_value=adapter), \
         patch("subprocess.run") as run:
        rc = cli.cmd_login(_args(tmp_path, agent="mystery"))

    assert rc == 1
    run.assert_not_called()
    assert "doesn't know how to sign" in capsys.readouterr().err
