from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class FilePolicy(str, Enum):
    TRACKED_ONLY = "tracked-only"
    TRACKED_AND_UNTRACKED = "tracked-and-untracked"
    ALL_NONIGNORED = "all-nonignored"


@dataclass(frozen=True)
class RepositoryConfig:
    root: Path | None = None
    respect_gitignore: bool = True
    file_policy: FilePolicy = FilePolicy.TRACKED_ONLY
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    use_default_ignores: bool = True
    default_ignores: tuple[str, ...] = ()
    collapse_structural_directories: bool = True


@dataclass(frozen=True)
class RepositoryContext:
    root: Path
    selection: Path
    config_path: Path | None
    git_available: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhysicalNode:
    path: str
    is_directory: bool
    size: int = 0
    extension: str = ""
    language: str | None = None
    git_status: str | None = None
    is_symlink: bool = False
    is_manifest: bool = False
    is_documentation: bool = False


@dataclass(frozen=True)
class IgnoredPath:
    path: str
    reason: str
    is_directory: bool = False


@dataclass(frozen=True)
class LogicalModule:
    name: str
    physical_path: str
    collapsed_segments: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    source_size: int = 0
    children: tuple["LogicalModule", ...] = ()


@dataclass(frozen=True)
class InspectionResult:
    context: RepositoryContext
    physical_nodes: tuple[PhysicalNode, ...]
    ignored_paths: tuple[IgnoredPath, ...]
    root_module: LogicalModule
