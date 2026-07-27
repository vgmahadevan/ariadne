from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from .models import FilePolicy, RepositoryConfig, RepositoryContext


class RepositoryError(ValueError):
    pass


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
