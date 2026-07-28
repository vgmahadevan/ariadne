from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigurationError
from .generation import (
    GenerationError,
    PersistenceError,
    ValidationError,
    weave_repository,
)
from .git import GitError
from .inspection import inspect_repository
from .llm import LLMBackend, ModelError
from .models import FilePolicy, LogicalModule
from .render import render_inspection
from .repository import RepositoryError


class _ProgressBar:
    width = 24

    def update(
        self, completed: int, total: int, module: LogicalModule
    ) -> None:
        ratio = completed / total if total else 1.0
        filled = min(self.width, int(ratio * self.width))
        bar = "#" * filled + "-" * (self.width - filled)
        percent = int(ratio * 100)
        state = "starting" if completed == 0 else module.physical_path
        print(
            f"ariadne: weaving [{bar}] {completed}/{total} "
            f"({percent:3d}%) {state}",
            file=sys.stderr,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ariadne")
    subparsers = parser.add_subparsers(dest="command")
    inspect_parser = subparsers.add_parser(
        "inspect", help="display the planned logical module hierarchy"
    )
    inspect_parser.add_argument("path", nargs="?")
    inspect_parser.add_argument("--config", type=Path)
    inspect_parser.add_argument("--root")
    inspect_parser.add_argument("--no-git", action="store_true")
    inspect_parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="add paths and ignored directories; repeat for all metadata",
    )
    policy = inspect_parser.add_mutually_exclusive_group()
    policy.add_argument("--tracked-only", action="store_true")
    policy.add_argument("--include-untracked", action="store_true")
    weave_parser = subparsers.add_parser(
        "weave", help="generate documentation for a logical module subtree"
    )
    weave_parser.add_argument("path", nargs="?")
    weave_parser.add_argument("--config", type=Path)
    weave_parser.add_argument("--root")
    weave_parser.add_argument("--no-git", action="store_true")
    weave_parser.add_argument(
        "--module-only",
        action="store_true",
        help="generate only the selected module, not its descendants",
    )
    weave_parser.add_argument(
        "--force",
        action="store_true",
        help="allow replacement of human-modified documentation",
    )
    weave_policy = weave_parser.add_mutually_exclusive_group()
    weave_policy.add_argument("--tracked-only", action="store_true")
    weave_policy.add_argument("--include-untracked", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    backend: LLMBackend | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage()
        return 0
    selected_policy = None
    if args.tracked_only:
        selected_policy = FilePolicy.TRACKED_ONLY
    elif args.include_untracked:
        selected_policy = FilePolicy.TRACKED_AND_UNTRACKED
    try:
        if args.command == "weave":
            progress = _ProgressBar()
            generated = weave_repository(
                path=args.path,
                config_path=args.config,
                root=args.root,
                git_enabled=not args.no_git,
                file_policy=selected_policy,
                module_only=args.module_only,
                force=args.force,
                backend=backend,
                on_config_created=lambda path: print(
                    f"ariadne: created default configuration at {path}; "
                    "review the model endpoint before retrying",
                    file=sys.stderr,
                ),
                on_progress=progress.update,
            )
            for item in generated:
                print(item.output_path)
            return 0
        result = inspect_repository(
            path=args.path,
            config_path=args.config,
            root=args.root,
            git_enabled=not args.no_git,
            file_policy=selected_policy,
        )
    except (
        ConfigurationError,
        RepositoryError,
        GitError,
        ModelError,
        GenerationError,
        ValidationError,
        PersistenceError,
        OSError,
    ) as exc:
        print(f"ariadne: error: {exc}", file=sys.stderr)
        return 2
    for warning in result.context.warnings:
        print(f"ariadne: warning: {warning}", file=sys.stderr)
    print(render_inspection(result, verbosity=min(args.verbose, 2)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
