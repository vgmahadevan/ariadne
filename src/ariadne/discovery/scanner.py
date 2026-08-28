from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pathspec import PathSpec

from ..settings import RepositoryConfig
from .models import IgnoredPath, PhysicalNode, RepositoryContext
from .repository import GitIndex

DEFAULT_IGNORED_NAMES = frozenset(
    {
        ".git", ".ariadne", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
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
LANGUAGES = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".scala": "Scala",
    ".sh": "Shell",
}


@dataclass(frozen=True)
class ScanResult:
    nodes: tuple[PhysicalNode, ...]
    ignored: tuple[IgnoredPath, ...]


def detect_language(path: Path) -> str | None:
    return LANGUAGES.get(path.suffix.lower())


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
            is_symlink = entry.is_symlink()
            is_dir = entry.is_dir(follow_symlinks=False)
            reported_is_dir = is_dir or (
                is_symlink and entry.is_dir(follow_symlinks=True)
            )
            match_path = rel + ("/" if is_dir else "")
            reason = _early_ignore(
                entry.name,
                match_path,
                is_symlink,
                config,
                exclude,
                extra_defaults,
            )
            if reason is None and git_index and config.respect_gitignore and git_index.is_ignored(rel):
                reason = "gitignore"
            if reason:
                ignored.append(
                    IgnoredPath(rel, reason, is_directory=reported_is_dir)
                )
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
            policy_reason = (
                git_index.policy_reason(
                    rel,
                    config.file_policy,
                    respect_gitignore=config.respect_gitignore,
                )
                if git_index
                else None
            )
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
    config: RepositoryConfig,
    exclude: PathSpec,
    extra_defaults: PathSpec,
) -> str | None:
    if is_symlink:
        return "symlink"
    if name.endswith("-genai-doc.md") or name.endswith("-genai-openapi.yaml"):
        return "generated-document"
    if re.search(
        r"(?i)(^test_.+_genai\.py$|_genai_test\.go$|"
        r"\.genai\.(?:test|spec)\.(?:js|ts)$|"
        r"genai(?:test|tests|spec)\.(?:java|kt|scala|cs|php|swift)$|"
        r"_genai(?:_test|_spec)?\.(?:rs|rb|c|cpp|bats)$)",
        name,
    ):
        return "generated-test"
    if config.use_default_ignores and name in DEFAULT_IGNORED_NAMES:
        return "default-ignore"
    if extra_defaults.match_file(match_path):
        return "default-ignore"
    if exclude.match_file(match_path):
        return "configured-exclude"
    return None


def _relative(path: Path, root: Path) -> str:
    value = path.absolute().relative_to(root.resolve())
    return "." if not value.parts else PurePosixPath(*value.parts).as_posix()
