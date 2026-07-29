from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
