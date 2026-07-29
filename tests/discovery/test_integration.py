from __future__ import annotations

from pathlib import Path

from ariadne.discovery import inspect_repository
from ariadne.discovery.render import render_inspection
from ariadne.settings import FilePolicy

from conftest import init_git


def test_java_fixture_has_stable_collapsed_hierarchy(fixture_repo) -> None:
    root: Path = fixture_repo("java-nested")
    init_git(root)

    first = inspect_repository(cwd=root)
    second = inspect_repository(cwd=root)
    output = render_inspection(first, verbosity=2)

    assert output == render_inspection(second, verbosity=2)
    assert "optimizer" in output
    assert "collapsed=main/java/com/example" in output
    assert "languages=Java" in output


def test_python_fixture_tracks_untracked_and_ignores_cache(fixture_repo) -> None:
    root: Path = fixture_repo("python-package")
    init_git(root)
    (root / "src" / "ariadne_sample" / "draft.py").write_text("DRAFT = True\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "cache.pyc").write_bytes(b"x")

    result = inspect_repository(cwd=root)
    ignored = {(item.path, item.reason) for item in result.ignored_paths}
    assert ("src/ariadne_sample/draft.py", "git-untracked") in ignored
    assert ("__pycache__", "default-ignore") in ignored

    included = inspect_repository(
        cwd=root, file_policy=FilePolicy.TRACKED_AND_UNTRACKED
    )
    assert "draft.py" not in {item.path for item in included.ignored_paths}


def test_subtree_selection_keeps_repository_relative_paths(fixture_repo) -> None:
    root: Path = fixture_repo("js-monorepo")
    init_git(root)
    result = inspect_repository(cwd=root, path="packages/web")
    assert result.root_module.physical_path == "packages/web"
    assert "packages/web" in render_inspection(result, verbosity=1)


def test_polyglot_fixture_retains_service_boundaries(fixture_repo) -> None:
    root: Path = fixture_repo("polyglot-services")
    init_git(root)
    result = inspect_repository(cwd=root)
    output = render_inspection(result, verbosity=2)
    assert "languages=Go" in output
    assert "languages=Python" in output
    assert "api" in output and "worker" in output
