from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..discovery.models import LogicalModule
from .retrieval import RetrievalSummary


class GenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContextFile:
    path: str
    content: str
    evidence: str
    truncated: bool = False


@dataclass(frozen=True)
class ModuleContext:
    repository_name: str
    repository_root: str
    source_commit: str | None
    module: LogicalModule
    ancestors: tuple[str, ...]
    tree: tuple[str, ...]
    files: tuple[ContextFile, ...]
    omissions: tuple[str, ...]


@dataclass(frozen=True)
class PlannedModule:
    module: LogicalModule
    ancestors: tuple[str, ...]
    parent_output: Path | None
    output: Path


@dataclass(frozen=True)
class GenerationResult:
    module_path: str
    output_path: Path
    status: str
    model: str | None = None
    attempts: int = 0
    error_kind: str | None = None
    error: str | None = None
    draft_path: Path | None = None
    error_status_code: int | None = None
    retryable: bool | None = None
    retrieval: RetrievalSummary = RetrievalSummary()


class ModuleStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    GENERATED = "generated"
    UPDATED = "updated"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass(frozen=True)
class WeaveSummary:
    generated: int = 0
    updated: int = 0
    failed: int = 0
    partial: int = 0


@dataclass(frozen=True)
class ProgressEvent:
    index: int
    total: int
    module: LogicalModule
    status: str
    attempt: int = 0
    error_kind: str | None = None


@dataclass(frozen=True)
class WeaveResult:
    run_id: str
    modules: tuple[GenerationResult, ...]
    summary: WeaveSummary
    manifest_path: Path

    @property
    def successful(self) -> tuple[GenerationResult, ...]:
        return tuple(
            item
            for item in self.modules
            if item.status in {ModuleStatus.GENERATED.value, ModuleStatus.UPDATED.value}
        )
