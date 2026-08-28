from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ariadne.weave.cleanup import clean_repository
from ariadne.weave.documents import compose_document, compose_openapi_document
from ariadne.discovery.models import LogicalModule
from ariadne.settings import AriadneConfig


def _generated(module: str, *, human_modified: bool = False) -> str:
    document = compose_document(
        f"# {Path(module).name}\n\nGenerated.",
        config=AriadneConfig(),
        module=LogicalModule(Path(module).name, module),
        generated_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        source_commit_value=None,
        model="fake",
    )
    if human_modified:
        document = document.replace("human_modified: false", "human_modified: true")
    return document


def test_clean_removes_only_owned_documents_in_selected_subtree(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "child").mkdir(parents=True)
    selected = tmp_path / "src" / "src-genai-doc.md"
    child = tmp_path / "src" / "child" / "child-genai-doc.md"
    outside = tmp_path / "outside-genai-doc.md"
    unrelated = tmp_path / "src" / "notes-genai-doc.md"
    selected.write_text(_generated("src"), encoding="utf-8")
    child.write_text(_generated("src/child"), encoding="utf-8")
    outside.write_text(_generated("."), encoding="utf-8")
    unrelated.write_text("# Human documentation\n", encoding="utf-8")

    result = clean_repository(
        cwd=tmp_path,
        root=str(tmp_path),
        path="src",
        git_enabled=False,
    )

    assert result.documents == (child, selected)
    assert not selected.exists()
    assert not child.exists()
    assert outside.exists()
    assert unrelated.exists()


def test_clean_dry_run_and_human_modification_protection(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    generated = tmp_path / "src" / "src-genai-doc.md"
    protected = tmp_path / "src" / "protected-genai-doc.md"
    generated.write_text(_generated("src"), encoding="utf-8")
    protected.write_text(_generated("src/protected", human_modified=True), encoding="utf-8")

    preview = clean_repository(
        cwd=tmp_path,
        root=str(tmp_path),
        path="src",
        git_enabled=False,
        dry_run=True,
    )
    assert preview.documents == (protected.parent / "src-genai-doc.md",)
    assert generated.exists()
    assert protected.exists()

    result = clean_repository(
        cwd=tmp_path,
        root=str(tmp_path),
        path="src",
        git_enabled=False,
        include_human_modified=True,
    )
    assert result.documents == (protected, generated)
    assert not generated.exists()
    assert not protected.exists()


def test_clean_drafts_requires_draft_provenance_and_respects_subtree(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    drafts = tmp_path / ".ariadne" / "drafts" / "run"
    drafts.mkdir(parents=True)
    selected = drafts / "src.md"
    outside = drafts / "other.md"
    ordinary = drafts / "ordinary.md"
    selected.write_text(
        _generated("src").replace("generated: true", "generated: true\n  draft: true"),
        encoding="utf-8",
    )
    outside.write_text(
        _generated("other").replace("generated: true", "generated: true\n  draft: true"),
        encoding="utf-8",
    )
    ordinary.write_text(_generated("src"), encoding="utf-8")

    result = clean_repository(
        cwd=tmp_path,
        root=str(tmp_path),
        path="src",
        git_enabled=False,
        include_drafts=True,
    )

    assert result.drafts == (selected,)
    assert not selected.exists()
    assert outside.exists()
    assert ordinary.exists()


def test_api_clean_is_independent_from_regular_documents(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    regular = tmp_path / "src" / "src-genai-doc.md"
    api = tmp_path / "src" / "src-genai-openapi.yaml"
    regular.write_text(_generated("src"), encoding="utf-8")
    api.write_text(
        compose_openapi_document(
            "openapi: 3.1.0\ninfo:\n  title: API\n  version: 1.0.0\npaths: {}\n",
            module=LogicalModule("src", "src"),
            generated_at=datetime.now(timezone.utc), source_commit_value=None,
            model="fake",
        ),
        encoding="utf-8",
    )

    result = clean_repository(
        cwd=tmp_path, root=str(tmp_path), path="src", git_enabled=False,
        artifact_type="openapi",
    )

    assert result.documents == (api,)
    assert not api.exists()
    assert regular.exists()

    result = clean_repository(
        cwd=tmp_path, root=str(tmp_path), path="src", git_enabled=False,
        artifact_type="all",
    )
    assert result.documents == (regular,)
    assert not regular.exists()


def test_clean_all_selects_docs_and_openapi_together(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    regular = tmp_path / "src" / "src-genai-doc.md"
    openapi = tmp_path / "src" / "src-genai-openapi.yaml"
    regular.write_text(_generated("src"), encoding="utf-8")
    openapi.write_text(
        compose_openapi_document(
            "openapi: 3.1.0\ninfo:\n  title: API\n  version: 1.0.0\npaths:\n  /health:\n    get:\n      responses:\n        '200':\n          description: Healthy\n",
            module=LogicalModule("src", "src"),
            generated_at=datetime.now(timezone.utc), source_commit_value=None,
            model="fake",
        ), encoding="utf-8",
    )

    result = clean_repository(
        cwd=tmp_path, root=str(tmp_path), path="src", git_enabled=False,
        artifact_type="all",
    )

    assert result.documents == (regular, openapi)
    assert not regular.exists()
    assert not openapi.exists()
