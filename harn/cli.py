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

    # setup → onboard automatically (unless suppressed), then point at watch.
    if not getattr(args, "no_onboard", False):
        print()
        _run_onboard(root, env_dir)

    print("\n[harn] Setup complete. To start working:")
    print("  • run `harn watch` (live status + Telegram routing) — keep it open;")
    print("  • then tell your agent: \"onboard this project\" — it reads "
          "harn_env/state/ONBOARD.md, maps the code, and fills the PRD + "
          "standards with you before building.")
    print("  • set [feedback] test_cmd in harn_env/harn.toml when you have tests.")
    return 0


def _agent_chain_names(env_dir: Path) -> list[str]:
    from .config import Config
    try:
        return Config.load(env_dir).agent_chain
    except Exception:
        return ["claude"]


def _set_require_mcp_false(env_dir: Path) -> None:
    toml = env_dir / "harn.toml"
    try:
        text = toml.read_text(encoding="utf-8")
    except OSError:
        return
    if "require_mcp" in text:
        import re
        text = re.sub(r"require_mcp\s*=\s*\w+", "require_mcp = false", text)
    else:
        text = text.replace("[harn]", "[harn]\nrequire_mcp = false", 1)
    toml.write_text(text, encoding="utf-8")


def _print_mcp_health(env_dir: Path, chain: list[str]) -> bool:
    """Verify the MCP server starts and tools respond; insist it's enabled.

    The server-side check is real. The agent-side toggle (Cursor) can't be
    inspected, so when `[harn] require_mcp` is on and we're interactive, we loop:
    show the enable steps and re-check until the user confirms or opts out — harn
    is useless to the agent until MCP is on.
    """
    from . import mcp_server
    from .config import Config
    try:
        require = Config.load(env_dir).require_mcp
    except Exception:
        require = True
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    needs_toggle = "cursor" in chain  # Cursor disables new MCP servers by default

    while True:
        print("\n[harn] checking the MCP server…")
        ok, tools, err = mcp_server.healthcheck(env_dir)
        if ok:
            print(f"   ✓ MCP server starts; {len(tools)} tools respond "
                  f"({', '.join(tools[:6])}…)")
        else:
            print(f"   ✗ MCP server problem: {err}")
        if needs_toggle:
            print("   ⚠ Cursor: Settings → MCP → toggle 'harn' ON. The config "
                  "file alone is NOT enough — Cursor disables new servers by default.")
        if "claude" in chain:
            print("   ℹ Claude Code: run `/mcp`; 'harn' must show as connected.")

        # Server OK and no manual toggle needed → done.
        if ok and not needs_toggle:
            return True
        if not require:
            return ok
        if not interactive:
            print("   ⚠ harn is useless until MCP is enabled. Enable it, then run "
                  "`harn doctor`. (Set [harn] require_mcp=false to silence.)")
            return ok
        ans = input("   → Enabled it in your agent? "
                    "[Enter = re-check · s = skip for now · n = don't ask again]: "
                    ).strip().lower()
        if ans == "s":
            print("   (skipped — run `harn doctor` once it's on)")
            return ok
        if ans == "n":
            _set_require_mcp_false(env_dir)
            print("   (disabled: [harn] require_mcp = false)")
            return ok
        # otherwise: loop and re-check


def _run_onboard(root: Path, env_dir: Path) -> None:
    """Analyse the project, seed skills, warm the index, write ONBOARD.md.
    Shared by `harn onboard` and `harn setup`."""
    from . import onboard, semble_bridge
    from .config import Config

    print("[harn] onboarding — analysing the project…")
    stack = onboard.detect_stack(root)
    print(f"   detected: {onboard.stack_summary(stack)}")

    notes = onboard.seed_skills(env_dir, stack)
    if notes:
        print("   seeded skills with obvious facts:")
        for n in notes:
            print(f"     • {n}")

    cfg = Config.load(env_dir)
    if cfg.code_search_semble and semble_bridge.semble_installed():
        print("   warming semble index (first run may take a bit)…")
        import subprocess
        cmd = semble_bridge.semble_server_cmd()
        if cmd and cmd[0].endswith("semble"):
            try:
                subprocess.run([cmd[0], "search", "project overview", str(root),
                                "-k", "1"], capture_output=True, timeout=300)
                print("     ✓ semble indexed")
            except Exception:
                print("     (semble warm-up skipped)")
    if cfg.code_search_socraticcode and semble_bridge.socraticcode_npx_available():
        print("   ℹ SocratiCode: ask the agent to run `codebase_index` "
              "(needs Docker running).")

    brief = onboard.onboard_brief(stack)
    (env_dir / "state").mkdir(exist_ok=True)
    (env_dir / "state" / "ONBOARD.md").write_text(
        f"# Auto-detected stack\n{onboard.stack_summary(stack)}\n\n{brief}",
        encoding="utf-8")

    # Ensure WORKFLOW.md exists and refresh ONLY its skills snapshot to reflect
    # the skills just seeded (never clobbering hand-edited steps).
    from . import workflow
    workflow.write(env_dir)            # create from template if missing
    workflow.refresh_skills(env_dir)   # update the snapshot block in place


def cmd_onboard(args) -> int:
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)
    if not env_dir.exists():
        print("[harn] no harn_env here. Run `harn setup` first.", file=sys.stderr)
        return 1
    _run_onboard(root, env_dir)
    print("\n[harn] Next: tell your agent \"onboard this project\" — it reads "
          "harn_env/state/ONBOARD.md, maps the code, and fills PRD + standards.")
    return 0


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


_REPO_URL = "https://github.com/bes-man/harn.git"


def cmd_update(args) -> int:
    """Update the harn package from GitHub. Never touches any harn_env/.

    harn_env/ lives inside your projects; the harn package lives in
    site-packages / pipx / a git checkout. Updating the code can't remove your
    project knowledge — they're in different places.
    """
    import subprocess
    import harn
    ref = args.ref
    pkg_dir = Path(harn.__file__).resolve().parent      # …/harn/harn
    repo = pkg_dir.parent                                # …/harn
    git_checkout = (repo / ".git").exists()

    print(f"[harn] updating harn (current {getattr(harn, '__version__', '?')})…")
    if git_checkout:
        print(f"[harn] editable/git checkout at {repo} → git pull")
        cmd = ["git", "-C", str(repo), "pull", "--ff-only"]
        if ref:
            cmd = ["git", "-C", str(repo), "fetch", "origin", ref, "&&",
                   "git", "-C", str(repo), "checkout", ref]
            r = subprocess.run(["git", "-C", str(repo), "fetch", "origin", ref])
            if r.returncode == 0:
                subprocess.run(["git", "-C", str(repo), "checkout", ref])
            r = subprocess.run(["git", "-C", str(repo), "pull", "--ff-only"])
        else:
            r = subprocess.run(cmd)
    else:
        url = _REPO_URL if not ref else f"{_REPO_URL}@{ref}"
        print(f"[harn] installed package → pip install --upgrade git+{url}")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade",
                            f"git+{url}"])
    if r.returncode != 0:
        print("[harn] update failed (see output above).", file=sys.stderr)
        return 1
    print("[harn] updated. Your harn_env/ folders are untouched.")

    # If run inside a project, refresh the harness-managed AGENTS.md so new
    # protocol rules actually reach the agent (it reads project_root/AGENTS.md).
    here = Path(args.path).resolve() if getattr(args, "path", None) else Path.cwd()
    if (here / ENV_DIRNAME).exists():
        try:
            from . import scaffold
            written = scaffold.refresh_agents_md(here)
            if written:
                print(f"[harn] refreshed AGENTS.md (backup: AGENTS.md.bak).")
            else:
                print("[harn] AGENTS.md already current.")
        except Exception as e:
            print(f"[harn] could not refresh AGENTS.md: {e}", file=sys.stderr)
    else:
        print("[harn] tip: run `harn update` from inside a project to also "
              "refresh its AGENTS.md with the latest protocol.")
    return 0


def cmd_run(args) -> int:
    from . import loop
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)
    if not env_dir.exists():
        print("[harn] no harn_env here. Run `harn setup` first.", file=sys.stderr)
        return 1
    loop.run(root, env_dir, max_iterations=args.max_iterations, auto=args.auto,
              only_task=args.task_id or None)
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


def cmd_ui(args) -> int:
    from . import studio
    env_dir = _env_dir(Path(args.path).resolve())
    if not env_dir.exists():
        print("[harn] no harn_env here. Run `harn setup` first.", file=sys.stderr)
        return 1
    studio.serve(env_dir, host=args.host, port=args.port,
                 open_browser=not args.no_open)
    return 0


def cmd_workflow(args) -> int:
    from . import workflow
    env_dir = _env_dir(Path(args.path).resolve())
    if args.refresh:
        p = workflow.refresh_skills(env_dir)
        print(f"[harn] refreshed skills snapshot in {p}")
        return 0
    if args.force:
        p = workflow.write(env_dir, force=True)
        print(f"[harn] reset WORKFLOW.md to the default template at {p}")
        return 0
    p = env_dir / workflow.FILENAME
    if not p.exists():
        workflow.write(env_dir)
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_explain(args) -> int:
    from . import loop
    from .config import Config
    env_dir = _env_dir(Path(args.path).resolve())
    print(loop.explain(Config.load(env_dir)))
    return 0


def cmd_trace(args) -> int:
    from . import events
    env_dir = _env_dir(Path(args.path).resolve())
    evs = events.read(env_dir, task_id=args.task_id or None,
                      run_id=args.run_id or None)
    if not evs:
        print("(no events recorded yet)")
        return 0
    for e in evs:
        ts = e.get("ts", "")
        ev = e.get("event", "")
        stage = e.get("stage", "")
        task = e.get("task_id", "")
        bits = [f"{ts}", f"{ev}"]
        if task:
            bits.append(task)
        if stage:
            bits.append(stage)
        for k in ("verdict", "outcome", "phase", "source", "kind", "name"):
            if e.get(k):
                bits.append(f"{k}={e[k]}")
        if e.get("dur_ms") is not None:
            bits.append(f"{e['dur_ms']}ms")
        toks = (e.get("tok_in") or 0) + (e.get("tok_out") or 0)
        if toks:
            bits.append(f"{toks}tok")
        line = "  ".join(str(b) for b in bits)
        if e.get("summary"):
            line += f"\n      └ {e['summary']}"
        print(line)
    return 0


def cmd_metrics(args) -> int:
    from . import events
    env_dir = _env_dir(Path(args.path).resolve())
    m = events.metrics(env_dir, run_id=args.run_id or None)
    if not m["events"]:
        print("(no events recorded yet)")
        return 0
    print(f"runs={m['runs']}  cycles={m['cycles']}  blocks={m['blocks']}  "
          f"errors={m['errors']}  events={m['events']}")
    print(f"tokens: {m['tok_in']} in + {m['tok_out']} out  "
          f"(~${m['cost_usd']})")
    o = m["oracle"]
    if any(o.values()):
        print(f"oracle: PASS={o['PASS']}  FAIL={o['FAIL']}  DEBT={o['DEBT']}")
    if m["stages"]:
        print("stages (turns · tokens · mean dur):")
        for name, s in m["stages"].items():
            print(f"  {name:<10} {s['count']:>3} · {s['tok']:>7}tok · "
                  f"{s['mean_dur_ms']:>6}ms")
    return 0


def cmd_changelog(args) -> int:
    from . import tasks
    env_dir = _env_dir(Path(args.path).resolve())
    md = tasks.render_changelog(env_dir, args.task_id)
    if args.write:
        out = env_dir / "CHANGELOG.md"
        out.write_text(md, encoding="utf-8")
        print(f"[harn] changelog written to {out}")
    else:
        print(md)
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
    sp.add_argument("--no-onboard", action="store_true",
                    help="don't run project analysis/onboarding after setup")
    sp.set_defaults(func=cmd_setup)

    rp = sub.add_parser("run", help="run the ralph loop")
    rp.add_argument("path", nargs="?", default=".")
    rp.add_argument("--max-iterations", type=int, default=None)
    rp.add_argument(
        "-a", "--auto", action="store_true",
        help="autonomous: decide without a human, more iterations, never touch "
             "harn_env .md files (not for complex tasks)",
    )
    rp.add_argument("--task", dest="task_id", default=None,
                    help="only work this task id, then stop (used by "
                         "harn ui's per-task Launch button)")
    rp.set_defaults(func=cmd_run)

    ap = sub.add_parser("answer", help="answer a blocked question and resume")
    ap.add_argument("text", help="your answer")
    ap.add_argument("path", nargs="?", default=".")
    ap.set_defaults(func=cmd_answer)

    up = sub.add_parser("update", help="update harn from GitHub (keeps harn_env)")
    up.add_argument("--ref", default="", help="branch/tag to install (default: main)")
    up.set_defaults(func=cmd_update)

    op = sub.add_parser(
        "onboard",
        help="analyse an existing project: detect stack, seed skills, warm the "
             "code index, brief the agent to fill PRD/standards",
    )
    op.add_argument("path", nargs="?", default=".")
    op.set_defaults(func=cmd_onboard)

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

    uip = sub.add_parser("ui",
                         help="visual editor for the workflow + skills (localhost)")
    uip.add_argument("--port", type=int, default=9999)
    uip.add_argument("--host", default="127.0.0.1")
    uip.add_argument("--no-open", action="store_true", help="don't open a browser")
    uip.add_argument("path", nargs="?", default=".")
    uip.set_defaults(func=cmd_ui)

    wfp = sub.add_parser("workflow",
                         help="show/refresh harn_env/WORKFLOW.md (the agent's flow)")
    wfp.add_argument("--refresh", action="store_true",
                     help="update only the skills snapshot, keep edits")
    wfp.add_argument("--force", action="store_true",
                     help="reset to the default template (discards edits)")
    wfp.add_argument("path", nargs="?", default=".")
    wfp.set_defaults(func=cmd_workflow)

    exp = sub.add_parser("explain",
                         help="show the pipeline stages that run for this config")
    exp.add_argument("path", nargs="?", default=".")
    exp.set_defaults(func=cmd_explain)

    trp = sub.add_parser("trace",
                         help="print the structured event stream (one run/task)")
    trp.add_argument("--task", dest="task_id", default=None, help="filter by task id")
    trp.add_argument("--run", dest="run_id", default=None, help="filter by run id")
    trp.add_argument("path", nargs="?", default=".")
    trp.set_defaults(func=cmd_trace)

    mtp = sub.add_parser("metrics",
                         help="aggregate observability numbers from the event stream")
    mtp.add_argument("--run", dest="run_id", default=None, help="limit to one run id")
    mtp.add_argument("path", nargs="?", default=".")
    mtp.set_defaults(func=cmd_metrics)

    cp = sub.add_parser("changelog",
                        help="assemble release notes from tasks' changelog + decisions")
    cp.add_argument("--task", dest="task_id", default=None,
                    help="limit to one task id (default: all)")
    cp.add_argument("--write", action="store_true",
                    help="also write to harn_env/CHANGELOG.md")
    cp.add_argument("path", nargs="?", default=".")
    cp.set_defaults(func=cmd_changelog)

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
