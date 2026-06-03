"""harn command-line interface."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ENV_DIRNAME, __version__


def _env_dir(project_root: Path) -> Path:
    return project_root / ENV_DIRNAME


def cmd_setup(args) -> int:
    from . import scaffold
    root = Path(args.path).resolve()
    result = scaffold.setup(root)
    print(f"[harn] harn_env created at {result['env_dir']}")
    for f in result["created"]:
        print(f"   + {f}")
    print("[harn] agent configs:")
    for c in result["agent_configs"]:
        print(f"   + {c}")
    print("\nNext: edit harn_env/skills/*, add tasks in harn_env/tasks/, "
          "set [feedback] test_cmd in harn_env/harn.toml, then `harn run`.")
    return 0


def cmd_run(args) -> int:
    from . import loop
    root = Path(args.path).resolve()
    env_dir = _env_dir(root)
    if not env_dir.exists():
        print("[harn] no harn_env here. Run `harn setup` first.", file=sys.stderr)
        return 1
    loop.run(root, env_dir, max_iterations=args.max_iterations)
    return 0


def cmd_answer(args) -> int:
    from . import loop
    env_dir = _env_dir(Path(args.path).resolve())
    loop.answer(env_dir, args.text)
    print("[harn] answer recorded; resume with `harn run`.")
    return 0


def cmd_status(args) -> int:
    from . import state
    env_dir = _env_dir(Path(args.path).resolve())
    st = state.State.load(env_dir / "state")
    print(f"phase: {st.phase}")
    print(f"current task: {st.current_task}")
    if st.question:
        print(f"waiting on: {st.question}")
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
    sp.set_defaults(func=cmd_setup)

    rp = sub.add_parser("run", help="run the ralph loop")
    rp.add_argument("path", nargs="?", default=".")
    rp.add_argument("--max-iterations", type=int, default=None)
    rp.set_defaults(func=cmd_run)

    ap = sub.add_parser("answer", help="answer a blocked question and resume")
    ap.add_argument("text", help="your answer")
    ap.add_argument("path", nargs="?", default=".")
    ap.set_defaults(func=cmd_answer)

    stp = sub.add_parser("status", help="show loop state")
    stp.add_argument("path", nargs="?", default=".")
    stp.set_defaults(func=cmd_status)

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
