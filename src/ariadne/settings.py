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


@dataclass(frozen=True)
class ModuleConfig:
    collapse_structural_directories: bool = True


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "openai-compatible"
    model: str = "local-model"
    endpoint: str = "http://localhost:8000/v1"
    context_window: int = 32768
    max_output_tokens: int = 6000
    temperature: float = 0.2
    timeout_seconds: float = 300.0
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ContextConfig:
    max_initial_tokens: int = 24000
    max_file_bytes: int = 100000
    max_tree_depth: int = 3
    include_parent_docs: bool = True
    include_generated_docs: bool = True
    characters_per_token: float = 4.0


@dataclass(frozen=True)
class GenerationConfig:
    output_suffix: str = "-genai-doc.md"
    overwrite_generated: bool = True
    overwrite_human_modified: bool = False
    max_concurrency: int = 8


@dataclass(frozen=True)
class RetrievalConfig:
    enabled: bool = True
    tools: tuple[str, ...] = (
        "list_directory",
        "read_file",
        "search_code",
        "get_module_tree",
    )
    max_tool_calls_per_module: int = 20
    max_identical_calls: int = 2
    max_result_bytes: int = 50000
    tool_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class AriadneConfig:
    repository: RepositoryConfig = RepositoryConfig()
    modules: ModuleConfig = ModuleConfig()
    model: ModelConfig = ModelConfig()
    context: ContextConfig = ContextConfig()
    generation: GenerationConfig = GenerationConfig()
    retrieval: RetrievalConfig = RetrievalConfig()
