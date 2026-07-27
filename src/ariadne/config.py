from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (
    AriadneConfig,
    ContextConfig,
    FilePolicy,
    GenerationConfig,
    ModelConfig,
    ModuleConfig,
    RepositoryConfig,
)


class ConfigurationError(ValueError):
    pass


DEFAULT_CONFIG = """\
# Ariadne uses an OpenAI-compatible chat-completions endpoint.
# Update the model name and endpoint before running `ariadne weave` again.
model:
  provider: openai-compatible
  model: local-model
  endpoint: http://localhost:8000/v1
  context_window: 32768
  max_output_tokens: 6000
  temperature: 0.2
  timeout_seconds: 300
  headers: {}

context:
  max_initial_tokens: 24000
  max_file_bytes: 100000
  max_tree_depth: 3
  include_parent_docs: true
  include_generated_docs: true
  characters_per_token: 4.0

generation:
  output_suffix: -genai-doc.md
  include_front_matter: true
  atomic_writes: true
  overwrite_generated: true
  overwrite_human_modified: false
"""


def discover_config(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / ".ariadne" / "config.yaml"
        if candidate.is_file():
            return candidate
    return None


def initialize_config(root: Path) -> Path:
    config_path = root / ".ariadne" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(DEFAULT_CONFIG)
    except FileExistsError:
        pass
    _ignore_ariadne_directory(root / ".gitignore")
    return config_path


def load_config(path: Path | None) -> AriadneConfig:
    if path is None:
        return AriadneConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("configuration must be a YAML mapping")
    repository = _mapping(data.get("repository", {}), "repository")
    modules = _mapping(data.get("modules", {}), "modules")
    model = _mapping(data.get("model", {}), "model")
    context = _mapping(data.get("context", {}), "context")
    generation = _mapping(data.get("generation", {}), "generation")
    _reject_unknown(
        data,
        {"repository", "modules", "model", "context", "generation"},
        "top-level",
    )
    allowed_repository = {
        "root", "respect_gitignore", "file_policy", "include", "exclude",
        "use_default_ignores", "default_ignores",
    }
    allowed_modules = {"collapse_structural_directories"}
    allowed_model = {
        "provider", "model", "endpoint", "context_window", "max_output_tokens",
        "temperature", "timeout_seconds", "headers",
    }
    allowed_context = {
        "max_initial_tokens", "max_file_bytes", "max_tree_depth",
        "include_parent_docs", "include_generated_docs", "characters_per_token",
    }
    allowed_generation = {
        "output_suffix", "include_front_matter", "atomic_writes",
        "overwrite_generated", "overwrite_human_modified",
    }
    _reject_unknown(repository, allowed_repository, "repository")
    _reject_unknown(modules, allowed_modules, "modules")
    _reject_unknown(model, allowed_model, "model")
    _reject_unknown(context, allowed_context, "context")
    _reject_unknown(generation, allowed_generation, "generation")
    root = repository.get("root")
    if root is not None and not isinstance(root, str):
        raise ConfigurationError("repository.root must be a string")
    try:
        policy = FilePolicy(repository.get("file_policy", FilePolicy.TRACKED_ONLY.value))
    except ValueError as exc:
        choices = ", ".join(item.value for item in FilePolicy)
        raise ConfigurationError(f"repository.file_policy must be one of: {choices}") from exc
    configured_root = None
    if root is not None:
        base = path.parent.parent if path.parent.name == ".ariadne" else path.parent
        configured_root = (base / root).resolve()
    headers = _mapping(model.get("headers", {}), "model.headers")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
        raise ConfigurationError("model.headers keys and values must be strings")
    provider = _string(model, "provider", "openai-compatible")
    if provider != "openai-compatible":
        raise ConfigurationError("model.provider must be openai-compatible")
    output_suffix = _string(generation, "output_suffix", "-genai-doc.md")
    if not output_suffix.endswith("-genai-doc.md") or "/" in output_suffix or "\\" in output_suffix:
        raise ConfigurationError(
            "generation.output_suffix must be a filename suffix ending in -genai-doc.md"
        )
    return AriadneConfig(
        repository=RepositoryConfig(
            root=configured_root,
            respect_gitignore=_boolean(repository, "respect_gitignore", True),
            file_policy=policy,
            include=_patterns(repository, "include"),
            exclude=_patterns(repository, "exclude"),
            use_default_ignores=_boolean(repository, "use_default_ignores", True),
            default_ignores=_patterns(repository, "default_ignores"),
        ),
        modules=ModuleConfig(
            collapse_structural_directories=_boolean(
                modules, "collapse_structural_directories", True
            )
        ),
        model=ModelConfig(
            provider=provider,
            model=_string(model, "model", "local-model"),
            endpoint=_string(model, "endpoint", "http://localhost:8000/v1").rstrip("/"),
            context_window=_positive_int(model, "context_window", 32768),
            max_output_tokens=_positive_int(model, "max_output_tokens", 6000),
            temperature=_nonnegative_number(model, "temperature", 0.2),
            timeout_seconds=_positive_number(model, "timeout_seconds", 300.0),
            headers=tuple(sorted(headers.items())),
        ),
        context=ContextConfig(
            max_initial_tokens=_positive_int(context, "max_initial_tokens", 24000),
            max_file_bytes=_positive_int(context, "max_file_bytes", 100000),
            max_tree_depth=_nonnegative_int(context, "max_tree_depth", 3),
            include_parent_docs=_boolean(context, "include_parent_docs", True),
            include_generated_docs=_boolean(context, "include_generated_docs", True),
            characters_per_token=_positive_number(
                context, "characters_per_token", 4.0
            ),
        ),
        generation=GenerationConfig(
            output_suffix=output_suffix,
            include_front_matter=_boolean(generation, "include_front_matter", True),
            atomic_writes=_boolean(generation, "atomic_writes", True),
            overwrite_generated=_boolean(
                generation, "overwrite_generated", True
            ),
            overwrite_human_modified=_boolean(
                generation, "overwrite_human_modified", False
            ),
        ),
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a mapping")
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigurationError(f"unknown {name} option(s): {', '.join(unknown)}")


def _boolean(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value


def _patterns(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be a list of strings")
    return tuple(value)


def _string(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{key} must be a nonempty string")
    return value


def _positive_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"{key} must be a positive integer")
    return value


def _nonnegative_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{key} must be a nonnegative integer")
    return value


def _positive_number(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"{key} must be a positive number")
    return float(value)


def _nonnegative_number(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{key} must be a nonnegative number")
    return float(value)


def _ignore_ariadne_directory(gitignore: Path) -> None:
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    except OSError as exc:
        raise ConfigurationError(f"cannot read {gitignore}: {exc}") from exc
    normalized = {
        line.strip().lstrip("/").rstrip("/")
        for line in existing.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if ".ariadne" in normalized:
        return
    prefix = "" if not existing or existing.endswith(("\n", "\r")) else "\n"
    try:
        with gitignore.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{prefix}.ariadne/\n")
    except OSError as exc:
        raise ConfigurationError(f"cannot update {gitignore}: {exc}") from exc
