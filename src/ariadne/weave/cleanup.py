from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import discover_config, load_config
from ..discovery import inspect_repository
from .documents import read_document_metadata


@dataclass(frozen=True)
class CleanResult:
    documents: tuple[Path, ...]
    drafts: tuple[Path, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        return (*self.documents, *self.drafts)


def clean_repository(
    *,
    cwd: Path | None = None,
    path: str | None = None,
    config_path: Path | None = None,
    root: str | None = None,
    git_enabled: bool = True,
    dry_run: bool = False,
    include_human_modified: bool = False,
    include_drafts: bool = False,
    api: bool = False,
) -> CleanResult:
    cwd = (cwd or Path.cwd()).resolve()
    config_start = Path(root).resolve() if root else cwd
    selected_config = (
        config_path.resolve() if config_path else discover_config(config_start)
    )
    config = load_config(selected_config)
    inspection = inspect_repository(
        cwd=cwd,
        path=path,
        config_path=selected_config,
        root=root,
        git_enabled=git_enabled,
    )
    repository_root = inspection.context.root.resolve()
    selection = inspection.context.selection.resolve()

    documents = tuple(
        candidate
        for candidate in _document_candidates(
            repository_root,
            selection,
            "-genai-api-doc.md" if api else config.generation.output_suffix,
            include_human_modified=include_human_modified,
        )
    )
    drafts = (
        tuple(
            _draft_candidates(
                repository_root,
                selection,
                include_human_modified=include_human_modified,
                api=api,
            )
        )
        if include_drafts
        else ()
    )
    if not dry_run:
        for candidate in (*documents, *drafts):
            candidate.unlink()
    return CleanResult(documents, drafts)


def _document_candidates(
    repository_root: Path,
    selection: Path,
    suffix: str,
    *,
    include_human_modified: bool,
) -> list[Path]:
    candidates: list[Path] = []
    for path in selection.rglob(f"*{suffix}"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if not _is_within(resolved, selection) or not _is_within(
            resolved, repository_root
        ):
            continue
        if _is_owned_artifact(
            path,
            include_human_modified=include_human_modified,
            require_draft=False,
        ):
            candidates.append(path)
    return sorted(candidates, key=lambda item: item.relative_to(repository_root).as_posix())


def _draft_candidates(
    repository_root: Path,
    selection: Path,
    *,
    include_human_modified: bool,
    api: bool,
) -> list[Path]:
    draft_root = repository_root / ".ariadne" / "drafts"
    if not draft_root.is_dir():
        return []
    selection_relative = selection.relative_to(repository_root).as_posix()
    if selection_relative == ".":
        selection_relative = "."
    candidates: list[Path] = []
    for path in draft_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        metadata = read_document_metadata(path)
        provenance = metadata.get("ariadne")
        if not isinstance(provenance, dict):
            continue
        document_type = provenance.get("document_type", "module")
        if (document_type == "api") is not api:
            continue
        logical_module = provenance.get("logical_module")
        if not isinstance(logical_module, str) or not _logical_within(
            logical_module, selection_relative
        ):
            continue
        if _is_owned_artifact(
            path,
            include_human_modified=include_human_modified,
            require_draft=True,
        ):
            candidates.append(path)
    return sorted(candidates, key=lambda item: item.relative_to(repository_root).as_posix())


def _is_owned_artifact(
    path: Path,
    *,
    include_human_modified: bool,
    require_draft: bool,
) -> bool:
    provenance = read_document_metadata(path).get("ariadne")
    if not isinstance(provenance, dict) or provenance.get("generated") is not True:
        return False
    if require_draft and provenance.get("draft") is not True:
        return False
    if not include_human_modified and (
        provenance.get("human_reviewed") is True
        or provenance.get("human_modified") is True
    ):
        return False
    return True


def _logical_within(module: str, selection: str) -> bool:
    return selection == "." or module == selection or module.startswith(selection + "/")


def _is_within(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents
