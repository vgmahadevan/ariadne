from pathlib import Path

import pytest

from ariadne.models import RepositoryConfig
from ariadne.repository import RepositoryError, resolve_repository

from conftest import init_git


def test_root_precedence_and_subtree_selection(tmp_path: Path) -> None:
    git_root = tmp_path / "git-root"
    nested = git_root / "src" / "pkg"
    nested.mkdir(parents=True)
    init_git(git_root)

    context, _ = resolve_repository(
        cwd=nested,
        selection_arg=".",
        config=RepositoryConfig(),
        config_path=None,
    )
    assert context.root == git_root.resolve()
    assert context.selection == nested.resolve()

    explicit = tmp_path / "explicit"
    explicit.mkdir()
    context, _ = resolve_repository(
        cwd=nested,
        selection_arg=None,
        config=RepositoryConfig(root=git_root),
        config_path=None,
        root_override=str(explicit),
        git_enabled=False,
    )
    assert context.root == explicit.resolve()


def test_rejects_selection_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(RepositoryError, match="outside"):
        resolve_repository(
            cwd=root,
            selection_arg="..",
            config=RepositoryConfig(),
            config_path=None,
            root_override=str(root),
            git_enabled=False,
        )
