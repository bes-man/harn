"""harn command-line interface."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ENV_DIRNAME, __version__


def _env_dir(project_root: Path) -> Path:
    return project_root / ENV_DIRNAME


def cmd_setup(args) -> int:
    from . import scaffold, semble_bridge
    from .config import Config
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)

    # Install enabled code-search backends BEFORE writing MCP configs, so an
    # installed semble lands in the generated config. Use the existing config if
    # the project was set up before, otherwise the defaults (both backends on).
    if not args.no_install:
        cfg = Config.load(env_dir) if (env_dir / "harn.toml").exists() else Config()
        statuses = semble_bridge.ensure_backends(cfg)
        if statuses:
            print("[harn] code search backends:")
            for line in statuses:
                print(f"   {line}")

    result = scaffold.setup(root)
    print(f"[harn] harn_env created at {result['env_dir']}")
    for f in result["created"]:
        print(f"   + {f}")
    print("[harn] agent connectors (root, git-ignored):")
    for c in result["agent_configs"]:
        print(f"   + {c}")

    _print_mcp_health(env_dir, _agent_chain_names(env_dir))

    print("\nNext: edit harn_env/skills/*, add tasks (the agent can via "
          "create_task), set [feedback] test_cmd in harn_env/harn.toml.")
    print("Then run `harn watch` in a terminal (live status + Telegram), and "
          "work with your agent or `harn run`.")
    return 0


def _agent_chain_names(env_dir: Path) -> list[str]:
    from .config import Config
    try:
        return Config.load(env_dir).agent_chain
    except Exception:
        return ["claude"]


def _print_mcp_health(env_dir: Path, chain: list[str]) -> bool:
    """Verify the MCP server starts and tools respond; print enable steps."""
    from . import mcp_server
    print("\n[harn] checking the MCP server…")
    ok, tools, err = mcp_server.healthcheck(env_dir)
    if ok:
        print(f"   ✓ MCP server starts; {len(tools)} tools respond "
              f"({', '.join(tools[:6])}…)")
    else:
        print(f"   ✗ MCP server problem: {err}")
        print("     Fix this first — agents can't use harn without it.")
    if "cursor" in chain:
        print("   ⚠ Cursor: open Settings → MCP and TOGGLE the 'harn' server ON "
              "(Cursor disables new MCP servers by default; the config file alone "
              "is not enough).")
    if "claude" in chain:
        print("   ℹ Claude Code: run `/mcp` to confirm 'harn' is connected.")
    return ok


def cmd_doctor(args) -> int:
    from . import mcp_server
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)
    if not env_dir.exists():
        print("[harn] no harn_env here. Run `harn setup` first.", file=sys.stderr)
        return 1
    ok = _print_mcp_health(env_dir, _agent_chain_names(env_dir))
    return 0 if ok else 1


def cmd_teardown(args) -> int:
    from . import scaffold
    root = Path(args.path).resolve()
    removed = scaffold.teardown(root)
    if removed:
        print("[harn] removed root connectors:")
        for r in removed:
            print(f"   - {r}")
        print("harn_env/ left intact. Re-create with `harn setup`.")
    else:
        print("[harn] nothing to remove in the project root.")
    return 0


def cmd_run(args) -> int:
    from . import loop
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)
    if not env_dir.exists():
        print("[harn] no harn_env here. Run `harn setup` first.", file=sys.stderr)
        return 1
    loop.run(root, env_dir, max_iterations=args.max_iterations, auto=args.auto)
    return 0


def cmd_answer(args) -> int:
    from . import loop
    env_dir = _env_dir(Path(args.path).resolve())
    loop.answer(env_dir, args.text)
    print("[harn] answer recorded; a running loop/`harn watch` resumes now, "
          "otherwise resume with `harn run`.")
    return 0


def cmd_rollback(args) -> int:
    from . import loop
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)
    res = loop.rollback(root, env_dir, args.task_id,
                        apply=args.apply, reopen=args.reopen)
    if not res.ok:
        print(f"[harn] rollback: {res.message}", file=sys.stderr)
        return 1
    print(f"[harn] {res.message}")
    if res.files:
        for f in res.files[:40]:
            print(f"    {f}")
        if len(res.files) > 40:
            print(f"    … and {len(res.files) - 40} more")
    if not args.apply and res.files:
        print("\n[harn] dry run. Re-run with --apply to restore "
              "(add --reopen to also reset the task to todo).")
    return 0


def cmd_watch(args) -> int:
    from . import loop
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)
    if not env_dir.exists():
        print("[harn] no harn_env here. Run `harn setup` first.", file=sys.stderr)
        return 1
    try:
        loop.watch(env_dir, root, poll_s=args.poll)
    except KeyboardInterrupt:
        print("\n[harn] watch stopped.")
    return 0


def cmd_status(args) -> int:
    from . import state, tasks
    env_dir = _env_dir(Path(args.path).resolve())
    st = state.State.load(env_dir / "state")
    print(f"phase: {st.phase}")
    print(f"current task: {st.current_task}")
    if st.question:
        print(f"waiting on: {st.question}")
    in_review = tasks.tasks_in_review(env_dir)
    if in_review:
        print("awaiting your review:")
        for t in in_review:
            print(f"  - {t.id}: {t.title}"
                  f"   (harn review {t.id} --approve | --changes \"…\")")
    return 0


def cmd_board(args) -> int:
    from . import tasks
    env_dir = _env_dir(Path(args.path).resolve())
    print(tasks.board(env_dir))
    return 0


def cmd_review(args) -> int:
    from . import loop
    env_dir = _env_dir(Path(args.path).resolve())
    if not args.approve and args.changes is None:
        print("[harn] pass --approve [--notes ...] or --changes \"...\"",
              file=sys.stderr)
        return 2
    try:
        outcome = loop.review(
            env_dir, args.task_id,
            approve=args.approve, notes=args.notes or "", changes=args.changes or "",
        )
    except ValueError as e:
        print(f"[harn] {e}", file=sys.stderr)
        return 1
    if outcome == "accepted":
        print(f"[harn] '{args.task_id}' accepted → done.")
    else:
        print(f"[harn] changes requested on '{args.task_id}'. Resume with `harn run`.")
    return 0


def cmd_mcp(args) -> int:
    from . import mcp_server
    mcp_server.serve(http=args.http, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harn", description="Agent-agnostic coding harness.")
    p.add_argument("--version", action="version", version=f"harn {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("setup", help="scaffold harn_env into a project")
    sp.add_argument("path", nargs="?", default=".", help="project root (default: .)")
    sp.add_argument("--no-install", action="store_true",
                    help="don't auto-install enabled code-search backends "
                         "(semble / SocratiCode prerequisites)")
    sp.set_defaults(func=cmd_setup)

    rp = sub.add_parser("run", help="run the ralph loop")
    rp.add_argument("path", nargs="?", default=".")
    rp.add_argument("--max-iterations", type=int, default=None)
    rp.add_argument(
        "-a", "--auto", action="store_true",
        help="autonomous: decide without a human, more iterations, never touch "
             "harn_env .md files (not for complex tasks)",
    )
    rp.set_defaults(func=cmd_run)

    ap = sub.add_parser("answer", help="answer a blocked question and resume")
    ap.add_argument("text", help="your answer")
    ap.add_argument("path", nargs="?", default=".")
    ap.set_defaults(func=cmd_answer)

    dp = sub.add_parser("doctor", help="verify the MCP server + tools work")
    dp.add_argument("path", nargs="?", default=".")
    dp.set_defaults(func=cmd_doctor)

    tp = sub.add_parser(
        "teardown",
        help="remove harn's root connectors (.mcp.json/.cursor/AGENTS.md); "
             "keeps harn_env/",
    )
    tp.add_argument("path", nargs="?", default=".")
    tp.set_defaults(func=cmd_teardown)

    rb = sub.add_parser(
        "rollback",
        help="restore the working tree to a task's baseline (redo from scratch)",
    )
    rb.add_argument("task_id")
    rb.add_argument("path", nargs="?", default=".")
    rb.add_argument("--apply", action="store_true",
                    help="actually restore files (default: dry run)")
    rb.add_argument("--reopen", action="store_true",
                    help="with --apply: also reset the task to todo (clears "
                         "scratchpad/decisions)")
    rb.set_defaults(func=cmd_rollback)

    wp = sub.add_parser(
        "watch",
        help="HIL coordinator for chat runs: escalate blocked questions to "
             "Telegram after the chat grace",
    )
    wp.add_argument("path", nargs="?", default=".")
    wp.add_argument("--poll", type=int, default=3, help="seconds between checks")
    wp.set_defaults(func=cmd_watch)

    stp = sub.add_parser("status", help="show loop state")
    stp.add_argument("path", nargs="?", default=".")
    stp.set_defaults(func=cmd_status)

    bp = sub.add_parser("board", help="show the task track (todo→review→done)")
    bp.add_argument("path", nargs="?", default=".")
    bp.set_defaults(func=cmd_board)

    vp = sub.add_parser("review", help="accept a task or request changes")
    vp.add_argument("task_id", help="task id (file stem) currently in review")
    vp.add_argument("--approve", action="store_true", help="accept → done")
    vp.add_argument("--notes", default="", help="notes for future agents (on accept)")
    vp.add_argument("--changes", default=None, help="requested changes (sends it back)")
    vp.add_argument("path", nargs="?", default=".")
    vp.set_defaults(func=cmd_review)

    mp = sub.add_parser("mcp", help="run the MCP server (stdio by default)")
    mp.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    mp.add_argument("--host", default="127.0.0.1")
    mp.add_argument("--port", type=int, default=8765)
    mp.set_defaults(func=cmd_mcp)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
