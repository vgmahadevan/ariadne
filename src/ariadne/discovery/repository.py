from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from ..settings import FilePolicy, RepositoryConfig
from .models import InspectionResult, RepositoryContext


class RepositoryError(ValueError):
    pass


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitIndex:
    tracked: frozenset[str]
    untracked: frozenset[str]
    ignored: frozenset[str]

    def status(self, path: str) -> str | None:
        if path in self.tracked:
            return "tracked"
        if path in self.untracked:
            return "untracked"
        if self.is_ignored(path):
            return "ignored"
        return None

    def is_ignored(self, path: str) -> bool:
        return any(
            path == item or path.startswith(item.rstrip("/") + "/")
            for item in self.ignored
        )

    def policy_reason(
        self,
        path: str,
        policy: FilePolicy,
        *,
        respect_gitignore: bool,
    ) -> str | None:
        if policy is FilePolicy.TRACKED_ONLY and path not in self.tracked:
            return "git-untracked" if not self.is_ignored(path) else "gitignore"
        if policy is FilePolicy.TRACKED_AND_UNTRACKED:
            if not respect_gitignore:
                return None
            if path not in self.tracked and path not in self.untracked:
                return "gitignore" if self.is_ignored(path) else "git-unlisted"
        return None


def resolve_repository(
    *,
    cwd: Path,
    selection_arg: str | None,
    config: RepositoryConfig,
    config_path: Path | None,
    root_override: str | None = None,
    git_enabled: bool = True,
) -> tuple[RepositoryContext, RepositoryConfig]:
    cwd = cwd.resolve()
    explicit_root = Path(root_override).resolve() if root_override else None
    root = explicit_root or config.root
    git_root = _git_root(cwd) if git_enabled else None
    root = (root or git_root or cwd).resolve()
    if not root.is_dir():
        raise RepositoryError(f"repository root is not a directory: {root}")

    selection = (cwd / selection_arg).resolve() if selection_arg else root
    if not selection.exists():
        raise RepositoryError(f"inspection path does not exist: {selection}")
    if not selection.is_dir():
        raise RepositoryError(f"inspection path is not a directory: {selection}")
    try:
        selection.relative_to(root)
    except ValueError as exc:
        raise RepositoryError(f"inspection path is outside repository root: {selection}") from exc

    warnings: list[str] = []
    git_available = bool(git_enabled and shutil.which("git") and _git_root(root) == root)
    effective = config
    if git_enabled and not git_available:
        warnings.append(
            "Git integration unavailable; using all-nonignored file policy."
        )
        effective = replace(config, file_policy=FilePolicy.ALL_NONIGNORED)
    elif not git_enabled:
        effective = replace(
            config,
            file_policy=FilePolicy.ALL_NONIGNORED,
            respect_gitignore=False,
        )
    return (
        RepositoryContext(
            root=root,
            selection=selection,
            config_path=config_path,
            git_available=git_available,
            warnings=tuple(warnings),
        ),
        effective,
    )


def read_git_index(root: Path) -> GitIndex:
    return GitIndex(
        tracked=frozenset(_git_paths(root, ["ls-files", "--cached", "-z"])),
        untracked=frozenset(
            _git_paths(root, ["ls-files", "--others", "--exclude-standard", "-z"])
        ),
        ignored=frozenset(
            _git_paths(
                root,
                [
                    "ls-files", "--others", "--ignored", "--exclude-standard",
                    "--directory", "-z",
                ],
            )
        ),
    )


def source_commit(inspection: InspectionResult) -> str | None:
    if not inspection.context.git_available:
        return None
    result = subprocess.run(
        [
            "git", "-c", "safe.directory=*", "-C",
            str(inspection.context.root), "rev-parse", "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_root(path: Path) -> Path | None:
    if not shutil.which("git"):
        return None
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _git_paths(root: Path, arguments: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise GitError(message or "Git command failed")
    return [
        item.decode(errors="surrogateescape").replace("\\", "/").rstrip("/")
        for item in result.stdout.split(b"\0")
        if item
    ]
