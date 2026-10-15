"""Command-line entry point: interactive TUI or a one-shot prompt."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__
from .config import PERMISSIONS, ConfigError, init_config, load


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fluxen", description="Fluxen: multi-model terminal coding agent")
    parser.add_argument("--version", action="version", version=f"fluxen {__version__}")
    parser.add_argument(
        "--backend", choices=("litellm", "codex"), default="litellm", help="Agent backend (default: litellm)"
    )
    parser.add_argument("-m", "--model", help="Configured model alias (e.g. gpt, claude, azure)")
    # Kept for compatibility with earlier versions; same as --model.
    parser.add_argument("--provider", dest="model", help=argparse.SUPPRESS)
    parser.add_argument("--permission", choices=PERMISSIONS, help="Override the configured permission mode")
    parser.add_argument("--continue", dest="resume", action="store_true", help="Resume the latest session")
    parser.add_argument("--resume", dest="session", metavar="ID", help="Resume a specific session (ID or prefix)")
    parser.add_argument("-p", "--prompt", help="Run one task without the TUI and print the reply")
    parser.add_argument("--init", action="store_true", help="Create user config and exit")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace directory (default: cwd)")
    args = parser.parse_args(argv)
    if not args.init and not args.workspace.is_dir():
        parser.error(f"workspace {args.workspace} is not an existing directory")
    return args


async def run_once(args: argparse.Namespace, settings) -> int:
    """Headless mode: reply on stdout, progress on stderr, approvals asked on the terminal."""
    from .codex import CodexAgent
    from .engine import Agent
    from .session import Sessions
    from .tools import Workspace

    workspace = Workspace(args.workspace, settings.max_output_chars, settings.shell_timeout)
    sessions = Sessions(args.workspace)
    if args.session and not sessions.resume(args.session):
        print(f"fluxen: no unique saved session matches '{args.session}'", file=sys.stderr)
        return 2
    if args.resume:
        sessions.resume()
    failed = False

    async def emit(kind: str, value: str):
        nonlocal failed
        if kind == "token":
            sys.stdout.write(value)
            sys.stdout.flush()
        elif kind == "error":
            failed = True
            print(f"\nerror: {value}", file=sys.stderr)
        elif kind == "tool":
            print(f"\n[{value.splitlines()[0][:200]}]", file=sys.stderr)

    async def approve(description: str) -> bool:
        if not sys.stdin.isatty():
            print("\nfluxen: approval needed but stdin is not a terminal; denied", file=sys.stderr)
            return False
        print(f"\n{description}", file=sys.stderr)
        answer = await asyncio.to_thread(input, "Approve? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    agent: Agent | CodexAgent
    if args.backend == "codex":
        agent = CodexAgent(args.model, settings, workspace, sessions, emit, approve)
    else:
        agent = Agent(settings.model(args.model), settings, workspace, sessions, emit, approve)
    try:
        await agent.run(args.prompt)
    except Exception as exc:
        print(f"\nfluxen: {exc}", file=sys.stderr)
        return 1
    finally:
        if hasattr(agent, "close"):
            await agent.close()
    print()
    return 1 if failed else 0


def main(argv: list[str] | None = None):
    args = parse(argv)
    if args.init:
        print(init_config())
        return
    try:
        settings = load()
        if args.permission:
            settings.permission = args.permission
        if args.backend == "litellm":
            settings.model(args.model)  # fail early on an unknown alias
    except (ConfigError, ValueError) as exc:
        sys.exit(f"fluxen: {exc}")
    if args.prompt is not None:
        try:
            sys.exit(asyncio.run(run_once(args, settings)))
        except KeyboardInterrupt:
            sys.exit(130)
    from .tui import Fluxen

    try:
        app = Fluxen(args.workspace, settings, args.model, args.session or args.resume, backend=args.backend)
    except ValueError as exc:
        sys.exit(f"fluxen: {exc}")
    app.run()


if __name__ == "__main__":
    main()
