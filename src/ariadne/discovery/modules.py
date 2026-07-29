from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .models import LogicalModule, PhysicalNode


@dataclass
class _Directory:
    path: str
    files: list[PhysicalNode] = field(default_factory=list)
    children: dict[str, "_Directory"] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name if self.path != "." else "."

    @property
    def meaningful_direct_files(self) -> bool:
        return any(
            node.language
            or node.is_manifest
            or node.is_documentation
            or _is_meaningful_config(node)
            or _is_test(node)
            for node in self.files
        )


def discover_modules(
    nodes: tuple[PhysicalNode, ...],
    selection_path: str,
    collapse: bool,
) -> LogicalModule:
    root = _Directory(selection_path)
    directories: dict[str, _Directory] = {selection_path: root}
    for node in nodes:
        if node.is_directory:
            directories.setdefault(node.path, _Directory(node.path))
    for path, directory in sorted(directories.items()):
        if path == selection_path:
            continue
        parent_path = _parent(path)
        parent = directories.get(parent_path)
        if parent is not None:
            parent.children[directory.name] = directory
    for node in nodes:
        if node.is_directory:
            continue
        parent = directories.get(_parent(node.path))
        if parent is not None:
            parent.files.append(node)

    _prune_empty_directories(root)
    return _to_module(root, collapse=collapse, is_root=True)


def _to_module(directory: _Directory, *, collapse: bool, is_root: bool) -> LogicalModule:
    collapsed: list[str] = []
    effective = directory
    if collapse and not is_root:
        while (
            not effective.meaningful_direct_files
            and not effective.files
            and len(effective.children) == 1
        ):
            collapsed.append(effective.name)
            effective = next(iter(effective.children.values()))

    child_modules = [
        _to_module(child, collapse=collapse, is_root=False)
        for child in effective.children.values()
    ]
    child_modules.sort(key=lambda child: child.physical_path)
    direct_languages = {node.language for node in effective.files if node.language}
    languages = direct_languages | {
        language for child in child_modules for language in child.languages
    }
    direct_size = sum(node.size for node in effective.files if node.language)
    return LogicalModule(
        name=_module_name(effective, is_root),
        physical_path=effective.path,
        collapsed_segments=tuple(collapsed),
        languages=tuple(sorted(languages)),
        source_size=direct_size + sum(child.source_size for child in child_modules),
        children=tuple(child_modules),
    )


def _prune_empty_directories(directory: _Directory) -> bool:
    directory.children = {
        name: child
        for name, child in sorted(directory.children.items())
        if _prune_empty_directories(child)
    }
    return bool(directory.files or directory.children)


def _module_name(directory: _Directory, is_root: bool) -> str:
    if is_root:
        return PurePosixPath(directory.path).name if directory.path != "." else "repository"
    return directory.name


def _parent(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "." if parent == "." else parent


def _is_meaningful_config(node: PhysicalNode) -> bool:
    name = PurePosixPath(node.path).name
    return name.startswith(".") and name not in {".gitignore", ".gitattributes"}


def _is_test(node: PhysicalNode) -> bool:
    parts = {part.lower() for part in PurePosixPath(node.path).parts}
    return bool(parts & {"test", "tests", "spec", "specs"})
