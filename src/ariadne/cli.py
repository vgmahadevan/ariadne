from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .cleanup import clean_repository
from .config import ConfigurationError
from .generation import (
    GenerationError,
    ModuleStatus,
    PersistenceError,
    ProgressEvent,
    ValidationError,
    weave_repository,
)
from .git import GitError
from .inspection import inspect_repository
from .llm import LLMBackend, ModelError
from .models import FilePolicy
from .render import render_inspection
from .repository import RepositoryError
from .state import StateError


class _ProgressBar:
    width = 24

    def update(self, event: ProgressEvent) -> None:
        completed = event.index
        total = event.total
        module = event.module
        ratio = completed / total if total else 1.0
        filled = min(self.width, int(ratio * self.width))
        bar = "#" * filled + "-" * (self.width - filled)
        percent = int(ratio * 100)
        detail = event.status
        if event.error_kind:
            detail = f"{detail}: {event.error_kind}"
        print(
            f"ariadne: weaving [{bar}] {completed}/{total} "
            f"({percent:3d}%) {module.physical_path} - {detail}",
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
    weave_parser.add_argument(
        "--resume",
        action="store_true",
        help="resume the latest interrupted or incomplete compatible weave",
    )
    weave_parser.add_argument(
        "--max-concurrency",
        type=int,
        help="maximum number of active model requests",
    )
    weave_policy = weave_parser.add_mutually_exclusive_group()
    weave_policy.add_argument("--tracked-only", action="store_true")
    weave_policy.add_argument("--include-untracked", action="store_true")
    clean_parser = subparsers.add_parser(
        "clean", help="safely remove Ariadne-generated documentation"
    )
    clean_parser.add_argument("path", nargs="?")
    clean_parser.add_argument("--config", type=Path)
    clean_parser.add_argument("--root")
    clean_parser.add_argument("--no-git", action="store_true")
    clean_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list generated artifacts without removing them",
    )
    clean_parser.add_argument(
        "--include-human-modified",
        action="store_true",
        help="also remove generated documents marked as human-reviewed or modified",
    )
    clean_parser.add_argument(
        "--drafts",
        action="store_true",
        help="also remove Ariadne partial drafts for the selected subtree",
    )
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
    if getattr(args, "tracked_only", False):
        selected_policy = FilePolicy.TRACKED_ONLY
    elif getattr(args, "include_untracked", False):
        selected_policy = FilePolicy.TRACKED_AND_UNTRACKED
    try:
        if args.command == "clean":
            result = clean_repository(
                path=args.path,
                config_path=args.config,
                root=args.root,
                git_enabled=not args.no_git,
                dry_run=args.dry_run,
                include_human_modified=args.include_human_modified,
                include_drafts=args.drafts,
            )
            for item in result.paths:
                print(item)
            action = "would remove" if args.dry_run else "removed"
            print(
                f"ariadne: {action} {len(result.paths)} generated artifact(s)",
                file=sys.stderr,
            )
            return 0
        if args.command == "weave":
            progress = _ProgressBar()
            generated = asyncio.run(
                weave_repository(
                    path=args.path,
                    config_path=args.config,
                    root=args.root,
                    git_enabled=not args.no_git,
                    file_policy=selected_policy,
                    module_only=args.module_only,
                    force=args.force,
                    resume=args.resume,
                    max_concurrency=args.max_concurrency,
                    backend=backend,
                    on_config_created=lambda path: print(
                        f"ariadne: created default configuration at {path}; "
                        "review the model endpoint before retrying",
                        file=sys.stderr,
                    ),
                    on_progress=progress.update,
                ),
            )
            for item in generated.successful:
                print(item.output_path)
            summary = generated.summary
            print(
                "ariadne: weave complete: "
                f"generated={summary.generated} updated={summary.updated} "
                f"failed={summary.failed} partial={summary.partial} "
                f"cancelled={summary.cancelled}",
                file=sys.stderr,
            )
            for item in generated.modules:
                if item.status in {
                    ModuleStatus.FAILED.value,
                    ModuleStatus.PARTIAL.value,
                }:
                    print(
                        f"ariadne: {item.module_path}: "
                        f"{item.error_kind or 'error'}: {item.error or ''}",
                        file=sys.stderr,
                    )
            return 1 if summary.failed or summary.partial else 0
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
        StateError,
        OSError,
    ) as exc:
        print(f"ariadne: error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ariadne: cancelled", file=sys.stderr)
        return 130
    for warning in result.context.warnings:
        print(f"ariadne: warning: {warning}", file=sys.stderr)
    print(render_inspection(result, verbosity=min(args.verbose, 2)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
