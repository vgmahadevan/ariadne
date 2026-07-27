from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pathspec import PathSpec

from .git import GitIndex
from .languages import detect_language
from .models import IgnoredPath, PhysicalNode, RepositoryConfig, RepositoryContext

DEFAULT_IGNORED_NAMES = frozenset(
    {
        ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "node_modules", "bower_components", "vendor", "target", "build", "dist",
        "out", "coverage", ".next", ".nuxt", ".gradle", ".idea", ".vscode",
    }
)
MANIFEST_NAMES = frozenset(
    {
        "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml",
        "build.gradle", "build.gradle.kts", "Gemfile", "CMakeLists.txt", "Makefile",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    }
)
DOCUMENT_NAMES = frozenset({"README", "README.md", "README.rst", "README.txt"})


@dataclass(frozen=True)
class ScanResult:
    nodes: tuple[PhysicalNode, ...]
    ignored: tuple[IgnoredPath, ...]


def scan_repository(
    context: RepositoryContext,
    config: RepositoryConfig,
    git_index: GitIndex | None,
) -> ScanResult:
    include = PathSpec.from_lines("gitwildmatch", config.include)
    exclude = PathSpec.from_lines("gitwildmatch", config.exclude)
    extra_defaults = PathSpec.from_lines("gitwildmatch", config.default_ignores)
    nodes: list[PhysicalNode] = []
    ignored: list[IgnoredPath] = []

    selection_rel = _relative(context.selection, context.root)

    def walk(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name.casefold()):
            path = Path(entry.path)
            rel = _relative(path, context.root)
            is_dir = entry.is_dir(follow_symlinks=False)
            match_path = rel + ("/" if is_dir else "")
            reason = _early_ignore(
                entry.name, match_path, entry.is_symlink(), is_dir, config, exclude, extra_defaults
            )
            if reason is None and git_index and config.respect_gitignore and git_index.is_ignored(rel):
                reason = "gitignore"
            if reason:
                ignored.append(IgnoredPath(rel, reason, is_directory=is_dir))
                continue
            if is_dir:
                nodes.append(
                    PhysicalNode(path=rel, is_directory=True, is_symlink=False)
                )
                walk(path)
                continue
            if entry.is_symlink():
                ignored.append(
                    IgnoredPath(
                        rel,
                        "symlink",
                        is_directory=entry.is_dir(follow_symlinks=True),
                    )
                )
                continue
            if config.include and not include.match_file(rel):
                ignored.append(IgnoredPath(rel, "not-included"))
                continue
            policy_reason = git_index.policy_reason(rel, config.file_policy) if git_index else None
            if policy_reason:
                ignored.append(IgnoredPath(rel, policy_reason))
                continue
            stat = entry.stat(follow_symlinks=False)
            filename = path.name
            nodes.append(
                PhysicalNode(
                    path=rel,
                    is_directory=False,
                    size=stat.st_size,
                    extension=path.suffix.lower(),
                    language=detect_language(path),
                    git_status=git_index.status(rel) if git_index else None,
                    is_manifest=filename in MANIFEST_NAMES or filename.endswith(".csproj"),
                    is_documentation=filename in DOCUMENT_NAMES,
                )
            )

    nodes.append(
        PhysicalNode(path=selection_rel, is_directory=True)
    )
    walk(context.selection)
    return ScanResult(
        nodes=tuple(sorted(nodes, key=lambda node: (node.path, not node.is_directory))),
        ignored=tuple(sorted(ignored, key=lambda item: (item.path, item.reason))),
    )


def _early_ignore(
    name: str,
    match_path: str,
    is_symlink: bool,
    is_directory: bool,
    config: RepositoryConfig,
    exclude: PathSpec,
    extra_defaults: PathSpec,
) -> str | None:
    if is_symlink:
        return "symlink"
    if config.use_default_ignores and name in DEFAULT_IGNORED_NAMES:
        return "default-ignore"
    if extra_defaults.match_file(match_path):
        return "default-ignore"
    if exclude.match_file(match_path):
        return "configured-exclude"
    return None


def _relative(path: Path, root: Path) -> str:
    value = path.resolve().relative_to(root.resolve())
    return "." if not value.parts else PurePosixPath(*value.parts).as_posix()
