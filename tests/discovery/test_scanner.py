from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.discovery.models import RepositoryContext
from ariadne.discovery.repository import read_git_index
from ariadne.discovery.scanner import scan_repository
from ariadne.settings import FilePolicy, RepositoryConfig

from conftest import init_git


def _context(root: Path, git: bool = False) -> RepositoryContext:
    return RepositoryContext(root, root, None, git)


def test_ignore_precedence_and_language_metadata(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / "src" / "skip.py").write_text("pass", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("", encoding="utf-8")

    result = scan_repository(
        _context(tmp_path),
        RepositoryConfig(
            file_policy=FilePolicy.ALL_NONIGNORED,
            include=("src/**",),
            exclude=("**/skip.py",),
        ),
        None,
    )

    files = {node.path: node for node in result.nodes if not node.is_directory}
    assert files["src/main.py"].language == "Python"
    assert {item.path: item.reason for item in result.ignored} == {
        "node_modules": "default-ignore",
        "src/skip.py": "configured-exclude",
    }


def test_git_file_policies_and_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("", encoding="utf-8")
    init_git(tmp_path)
    (tmp_path / "new.py").write_text("", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("", encoding="utf-8")
    index = read_git_index(tmp_path)

    tracked = scan_repository(
        _context(tmp_path, True),
        RepositoryConfig(file_policy=FilePolicy.TRACKED_ONLY),
        index,
    )
    assert "tracked.py" in {node.path for node in tracked.nodes}
    assert ("new.py", "git-untracked") in {
        (item.path, item.reason) for item in tracked.ignored
    }

    combined = scan_repository(
        _context(tmp_path, True),
        RepositoryConfig(file_policy=FilePolicy.TRACKED_AND_UNTRACKED),
        index,
    )
    paths = {node.path for node in combined.nodes}
    assert {"tracked.py", "new.py"} <= paths
    assert ("ignored.py", "gitignore") in {
        (item.path, item.reason) for item in combined.ignored
    }

    ignoring_gitignore = scan_repository(
        _context(tmp_path, True),
        RepositoryConfig(
            file_policy=FilePolicy.TRACKED_AND_UNTRACKED,
            respect_gitignore=False,
        ),
        index,
    )
    assert "ignored.py" in {node.path for node in ignoring_gitignore.nodes}


def test_symlink_is_not_followed_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available")
    result = scan_repository(
        _context(root),
        RepositoryConfig(file_policy=FilePolicy.ALL_NONIGNORED),
        None,
    )
    ignored = next(item for item in result.ignored if item.path == "linked")
    assert ignored.reason == "symlink"
    assert ignored.is_directory
