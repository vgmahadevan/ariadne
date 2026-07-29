from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..config import discover_config, load_config
from ..settings import FilePolicy
from .models import InspectionResult, LogicalModule
from .modules import discover_modules
from .repository import read_git_index, resolve_repository
from .scanner import scan_repository

__all__ = ["InspectionResult", "LogicalModule", "inspect_repository"]


def inspect_repository(
    *,
    cwd: Path | None = None,
    path: str | None = None,
    config_path: Path | None = None,
    root: str | None = None,
    git_enabled: bool = True,
    file_policy: FilePolicy | None = None,
) -> InspectionResult:
    cwd = (cwd or Path.cwd()).resolve()
    config_start = Path(root).resolve() if root else cwd
    selected_config = (
        config_path.resolve() if config_path else discover_config(config_start)
    )
    ariadne_config = load_config(selected_config)
    config = ariadne_config.repository
    if file_policy is not None:
        config = replace(config, file_policy=file_policy)
    context, config = resolve_repository(
        cwd=cwd,
        selection_arg=path,
        config=config,
        config_path=selected_config,
        root_override=root,
        git_enabled=git_enabled,
    )
    git_index = read_git_index(context.root) if context.git_available else None
    scan = scan_repository(context, config, git_index)
    selection_rel = context.selection.relative_to(context.root).as_posix() or "."
    module = discover_modules(
        scan.nodes,
        selection_path=selection_rel,
        collapse=ariadne_config.modules.collapse_structural_directories,
    )
    return InspectionResult(
        context=context,
        physical_nodes=scan.nodes,
        ignored_paths=scan.ignored,
        root_module=module,
    )
