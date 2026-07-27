from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import FilePolicy, RepositoryConfig


class ConfigurationError(ValueError):
    pass


def discover_config(start: Path) -> Path | None:
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / ".ariadne" / "config.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None) -> RepositoryConfig:
    if path is None:
        return RepositoryConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("configuration must be a YAML mapping")
    repository = _mapping(data.get("repository", {}), "repository")
    modules = _mapping(data.get("modules", {}), "modules")
    allowed_repository = {
        "root", "respect_gitignore", "file_policy", "include", "exclude",
        "use_default_ignores", "default_ignores",
    }
    allowed_modules = {"collapse_structural_directories"}
    _reject_unknown(repository, allowed_repository, "repository")
    _reject_unknown(modules, allowed_modules, "modules")
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
    return RepositoryConfig(
        root=configured_root,
        respect_gitignore=_boolean(repository, "respect_gitignore", True),
        file_policy=policy,
        include=_patterns(repository, "include"),
        exclude=_patterns(repository, "exclude"),
        use_default_ignores=_boolean(repository, "use_default_ignores", True),
        default_ignores=_patterns(repository, "default_ignores"),
        collapse_structural_directories=_boolean(
            modules, "collapse_structural_directories", True
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
