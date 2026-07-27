from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigurationError
from .git import GitError
from .inspection import inspect_repository
from .models import FilePolicy
from .render import render_inspection
from .repository import RepositoryError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ariadne")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser(
        "inspect", help="display the planned logical module hierarchy"
    )
    inspect_parser.add_argument("path", nargs="?")
    inspect_parser.add_argument("--config", type=Path)
    inspect_parser.add_argument("--root")
    inspect_parser.add_argument("--no-git", action="store_true")
    policy = inspect_parser.add_mutually_exclusive_group()
    policy.add_argument("--tracked-only", action="store_true")
    policy.add_argument("--include-untracked", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    selected_policy = None
    if args.tracked_only:
        selected_policy = FilePolicy.TRACKED_ONLY
    elif args.include_untracked:
        selected_policy = FilePolicy.TRACKED_AND_UNTRACKED
    try:
        result = inspect_repository(
            path=args.path,
            config_path=args.config,
            root=args.root,
            git_enabled=not args.no_git,
            file_policy=selected_policy,
        )
    except (ConfigurationError, RepositoryError, GitError, OSError) as exc:
        print(f"ariadne: error: {exc}", file=sys.stderr)
        return 2
    for warning in result.context.warnings:
        print(f"ariadne: warning: {warning}", file=sys.stderr)
    print(render_inspection(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
