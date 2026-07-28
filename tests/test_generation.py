from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from ariadne.generation import (
    PersistenceError,
    PlannedModule,
    ValidationError,
    assemble_context,
    build_prompt,
    compose_document,
    output_path,
    persist_document,
    plan_modules,
    top_down_modules,
    validate_document,
    weave_repository,
)
from ariadne.inspection import inspect_repository
from ariadne.llm import ModelError, ModelErrorKind, ModelRequest, ModelResponse
from ariadne.models import (
    AriadneConfig,
    FilePolicy,
    InspectionResult,
    LogicalModule,
    RepositoryContext,
)


class FakeBackend:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            "# Generated Module\n\n## Summary\n\nGrounded fixture documentation.",
            "fake-model",
        )


class FailingBackend:
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise ModelError(ModelErrorKind.CONNECTION, "endpoint could not be reached.")


def _config(root: Path) -> Path:
    path = root / ".ariadne" / "config.yaml"
    path.parent.mkdir()
    path.write_text(
        """
repository:
  file_policy: all-nonignored
model:
  provider: openai-compatible
  model: fake-model
  endpoint: http://unused/v1
  headers:
    X-Test: fixture
context:
  max_initial_tokens: 1000
  max_file_bytes: 1000
  characters_per_token: 4
generation:
  output_suffix: -genai-doc.md
""",
        encoding="utf-8",
    )
    return path


def test_top_down_traversal_and_output_naming() -> None:
    leaf = LogicalModule("Leaf API", "src/leaf")
    child = LogicalModule("Child", "src", children=(leaf,))
    root = LogicalModule("repository", ".", children=(child,))

    assert [item[0].name for item in top_down_modules(root)] == [
        "repository", "Child", "Leaf API"
    ]
    assert [item[0].name for item in top_down_modules(root, module_only=True)] == [
        "repository"
    ]
    assert output_path(Path("/repo"), leaf, "-genai-doc.md") == (
        Path("/repo/src/leaf/leaf-api-genai-doc.md")
    )


def test_output_collisions_are_disambiguated_deterministically(tmp_path: Path) -> None:
    first = LogicalModule("API!", "shared")
    second = LogicalModule("API?", "shared")
    inspection = InspectionResult(
        context=RepositoryContext(tmp_path, tmp_path, None, False),
        physical_nodes=(),
        ignored_paths=(),
        root_module=LogicalModule("repository", ".", children=(first, second)),
    )

    plans = plan_modules(inspection, AriadneConfig(), module_only=False)

    outputs = [plan.output.name for plan in plans[1:]]
    assert len(set(outputs)) == 2
    assert all(name.startswith("api-") and name.endswith("-genai-doc.md") for name in outputs)


def test_context_labels_generated_docs_as_unverified(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    prior = tmp_path / "src" / "src-genai-doc.md"
    prior.write_text("# Prior claim\n", encoding="utf-8")
    inspection = inspect_repository(
        cwd=tmp_path,
        path="src",
        git_enabled=False,
        file_policy=FilePolicy.ALL_NONIGNORED,
    )
    config = AriadneConfig()
    plan = PlannedModule(
        inspection.root_module,
        (),
        None,
        prior,
    )

    context = assemble_context(inspection, plan, config)
    prompt = build_prompt(context)

    assert any(item.path == "src/main.py" for item in context.files)
    generated = next(item for item in context.files if item.path.endswith("-genai-doc.md"))
    assert "unverified" in generated.evidence
    assert "must never override source evidence" in prompt.system_prompt
    assert "Prior claim" in prompt.user_prompt
    assert "src-genai-doc.md" not in {
        node.path for node in inspection.physical_nodes
    }


def test_prompt_adds_sibling_file_detail_guidance_only_for_leaf_modules(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
    (tmp_path / "src" / "second.py").write_text("SECOND = 2\n", encoding="utf-8")
    inspection = inspect_repository(
        cwd=tmp_path,
        path="src",
        git_enabled=False,
        file_policy=FilePolicy.ALL_NONIGNORED,
    )
    output = tmp_path / "src" / "src-genai-doc.md"
    leaf_context = assemble_context(
        inspection,
        PlannedModule(inspection.root_module, (), None, output),
        AriadneConfig(),
    )

    assert "additional detail about the sibling files" in (
        build_prompt(leaf_context).user_prompt
    )

    parent = LogicalModule(
        "src",
        "src",
        children=(LogicalModule("child", "src/child"),),
    )
    parent_context = assemble_context(
        inspection,
        PlannedModule(parent, (), None, output),
        AriadneConfig(),
    )

    assert "additional detail about the sibling files" not in (
        build_prompt(parent_context).user_prompt
    )


def test_compose_and_validate_provenance() -> None:
    config = AriadneConfig()
    document = compose_document(
        "# Module\n\n## Summary\n\nText.",
        config=config,
        module=LogicalModule("module", "src/module"),
        generated_at=datetime(2026, 7, 26, 20, tzinfo=timezone.utc),
        source_commit_value="abc123",
        model="fake",
    )

    validate_document(document)
    assert "2026-07-26T20:00:00+00:00" in document
    assert "source commit `abc123`" in document
    assert "human_modified: false" in document

    with pytest.raises(ValidationError, match="title"):
        validate_document(document.replace("# Module", "Module"))


def test_atomic_persistence_protects_human_changes(tmp_path: Path) -> None:
    destination = tmp_path / "module-genai-doc.md"
    config = AriadneConfig()
    original = compose_document(
        "# Module\n",
        config=config,
        module=LogicalModule("module", "."),
        generated_at=datetime.now(timezone.utc),
        source_commit_value=None,
        model="fake",
    ).replace("human_modified: false", "human_modified: true")
    destination.write_text(original, encoding="utf-8")

    with pytest.raises(PersistenceError, match="human-modified"):
        persist_document(destination, "replacement", config=config)
    assert destination.read_text(encoding="utf-8") == original

    persist_document(destination, "replacement", config=config, force=True)
    assert destination.read_text(encoding="utf-8") == "replacement"
    assert not list(tmp_path.glob("*.tmp"))


def test_weave_generates_subtree_and_module_only(tmp_path: Path) -> None:
    (tmp_path / "src" / "child").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src" / "child" / "api.py").write_text(
        "def call(): pass\n", encoding="utf-8"
    )
    config_path = _config(tmp_path)
    backend = FakeBackend()
    progress: list[tuple[int, int, str]] = []

    results = weave_repository(
        cwd=tmp_path,
        path="src",
        config_path=config_path,
        git_enabled=False,
        backend=backend,
        now=lambda: datetime(2026, 7, 26, tzinfo=timezone.utc),
        on_progress=lambda completed, total, module: progress.append(
            (completed, total, module.physical_path)
        ),
    )

    assert len(results) == 2
    assert all(item.output_path.is_file() for item in results)
    assert len(backend.requests) == 2
    assert progress == [(0, 2, "src"), (1, 2, "src"), (2, 2, "src/child")]
    assert "prior AI-generated documentation; unverified" in (
        backend.requests[1].user_prompt
    )

    other = tmp_path / "other"
    other.mkdir()
    (other / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    only_backend = FakeBackend()
    only = weave_repository(
        cwd=tmp_path,
        path="other",
        config_path=config_path,
        git_enabled=False,
        module_only=True,
        backend=only_backend,
    )
    assert len(only) == 1
    assert len(only_backend.requests) == 1


def test_weave_model_error_points_to_configuration(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_path = _config(tmp_path)

    with pytest.raises(ModelError) as captured:
        weave_repository(
            cwd=tmp_path,
            path="src",
            config_path=config_path,
            git_enabled=False,
            backend=FailingBackend(),
        )

    message = str(captured.value)
    assert str(config_path) in message
    assert "OpenAI-compatible chat-completions service" in message
    assert "model names and headers" in message
