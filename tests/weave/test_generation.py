"""Planning, context, document, and basic weave coverage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ariadne.weave.context import assemble_context, build_prompt
from ariadne.weave.documents import (
    PersistenceError,
    ValidationError,
    compose_document,
    persist_document,
    validate_document,
    read_document_metadata,
)
from ariadne.weave.models import PlannedModule
from ariadne.weave.planning import plan_modules
from ariadne.weave.runner import weave_repository
from ariadne.discovery import inspect_repository
from ariadne.discovery.models import (
    InspectionResult,
    LogicalModule,
    RepositoryContext,
)
from ariadne.llm import ModelError, ModelErrorKind, ModelRequest, ModelResponse
from ariadne.settings import (
    AriadneConfig,
    ContextConfig,
    FilePolicy,
)


class FakeBackend:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            "# Generated Module\n\n## Summary\n\nGrounded fixture documentation.",
            "fake-model",
        )


class FailingBackend:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise ModelError(ModelErrorKind.CONNECTION, "endpoint could not be reached.")


class ApiBackend(FakeBackend):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        marker = "<!-- ariadne-api: true -->\n" if len(self.requests) == 1 else ""
        return ModelResponse(
            marker + "# HTTP API\n\n## Routes\n\n`POST /widgets` accepts a widget.",
            "fake-model",
        )


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

    inspection = InspectionResult(
        context=RepositoryContext(Path("/repo"), Path("/repo"), None, False),
        physical_nodes=(),
        ignored_paths=(),
        root_module=root,
    )
    plans = plan_modules(inspection, AriadneConfig(), module_only=False)
    only = plan_modules(inspection, AriadneConfig(), module_only=True)

    assert [item.module.name for item in plans] == [
        "repository", "Child", "Leaf API"
    ]
    assert [item.module.name for item in only] == ["repository"]
    assert plans[-1].output == Path("/repo/src/leaf/leaf-api-genai-doc.md")


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


def test_context_respects_parent_document_setting(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    parent = tmp_path / "repository-genai-doc.md"
    parent.write_text("# Parent context\n", encoding="utf-8")
    inspection = inspect_repository(
        cwd=tmp_path,
        path="src",
        git_enabled=False,
        file_policy=FilePolicy.ALL_NONIGNORED,
    )
    plan = PlannedModule(
        inspection.root_module,
        (),
        parent,
        tmp_path / "src" / "src-genai-doc.md",
    )

    included = assemble_context(inspection, plan, AriadneConfig())
    excluded = assemble_context(
        inspection,
        plan,
        AriadneConfig(
            context=ContextConfig(include_parent_docs=False)
        ),
    )

    assert any(item.path == "repository-genai-doc.md" for item in included.files)
    assert all(item.path != "repository-genai-doc.md" for item in excluded.files)


def test_context_truncates_large_files_and_omits_binary_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "large.py").write_text("abcdefghij", encoding="utf-8")
    (tmp_path / "src" / "binary.bin").write_bytes(b"abc\x00def")
    inspection = inspect_repository(
        cwd=tmp_path,
        path="src",
        git_enabled=False,
        file_policy=FilePolicy.ALL_NONIGNORED,
    )
    config = AriadneConfig(
        context=ContextConfig(max_file_bytes=5)
    )
    context = assemble_context(
        inspection,
        PlannedModule(
            inspection.root_module,
            (),
            None,
            tmp_path / "src" / "src-genai-doc.md",
        ),
        config,
    )

    large = next(item for item in context.files if item.path == "src/large.py")
    assert large.content == "abcde"
    assert large.truncated
    assert any(
        omission == "- src/binary.bin: binary content omitted"
        for omission in context.omissions
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

    results = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="src",
            config_path=config_path,
            git_enabled=False,
            backend=backend,
            now=lambda: datetime(2026, 7, 26, tzinfo=timezone.utc),
            on_progress=lambda event: progress.append(
                (event.index, event.total, event.module.physical_path)
            ),
        ),
    )

    assert len(results.modules) == 2
    assert all(item.output_path.is_file() for item in results.successful)
    assert len(backend.requests) == 2
    assert progress == [
        (0, 2, "src"),
        (1, 2, "src"),
        (2, 2, "src/child"),
    ]
    assert "prior AI-generated documentation; unverified" in (
        backend.requests[1].user_prompt
    )

    other = tmp_path / "other"
    other.mkdir()
    (other / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    only_backend = FakeBackend()
    only = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="other",
            config_path=config_path,
            git_enabled=False,
            module_only=True,
            backend=only_backend,
        )
    )
    assert len(only.modules) == 1
    assert len(only_backend.requests) == 1


def test_api_weave_selects_marked_modules_and_writes_distinct_document(
    tmp_path: Path,
) -> None:
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "routes.py").write_text(
        '@app.post("/widgets")\ndef create_widget(): pass\n', encoding="utf-8"
    )
    config_path = _config(tmp_path)
    backend = ApiBackend()

    regular = asyncio.run(
        weave_repository(
            cwd=tmp_path, path="service", config_path=config_path,
            git_enabled=False, module_only=True, backend=backend,
        )
    )
    regular_path = regular.successful[0].output_path
    provenance = read_document_metadata(regular_path)["ariadne"]
    assert provenance["api"] is True
    assert provenance["document_type"] == "module"
    assert "ariadne-api" not in regular_path.read_text(encoding="utf-8")

    api_result = asyncio.run(
        weave_repository(
            cwd=tmp_path, path="service", config_path=config_path,
            git_enabled=False, module_only=True, api=True, backend=backend,
        )
    )
    api_path = tmp_path / "service" / "service-genai-api-doc.md"
    assert api_result.successful[0].output_path == api_path
    assert api_path.is_file()
    api_provenance = read_document_metadata(api_path)["ariadne"]
    assert api_provenance["document_type"] == "api"
    assert "Enumerate every supported route" in backend.requests[1].user_prompt


def test_api_weave_is_empty_when_no_module_is_marked(tmp_path: Path) -> None:
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_path = _config(tmp_path)
    backend = FakeBackend()
    asyncio.run(
        weave_repository(
            cwd=tmp_path, path="library", config_path=config_path,
            git_enabled=False, module_only=True, backend=backend,
        )
    )

    result = asyncio.run(
        weave_repository(
            cwd=tmp_path, path="library", config_path=config_path,
            git_enabled=False, module_only=True, api=True, backend=backend,
        )
    )
    assert result.modules == ()
    assert len(backend.requests) == 1


def test_weave_model_error_is_isolated_and_retried(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_path = _config(tmp_path)

    async def no_sleep(delay: float) -> None:
        pass

    result = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="src",
            config_path=config_path,
            git_enabled=False,
            backend=FailingBackend(),
            sleep=no_sleep,
        )
    )

    assert result.summary.failed == 1
    assert result.modules[0].error_kind == "connection"
    assert result.modules[0].attempts == 2


def test_invalid_regeneration_preserves_existing_document(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    config_path = _config(tmp_path)
    destination = tmp_path / "src" / "src-genai-doc.md"
    existing = compose_document(
        "# Existing Module\n\n## Summary\n\nKeep this document.",
        config=AriadneConfig(),
        module=LogicalModule("src", "src"),
        generated_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
        source_commit_value=None,
        model="previous-model",
    )
    destination.write_text(existing, encoding="utf-8")

    class InvalidBackend:
        async def generate(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse("This response has no title.", "invalid-model")

    result = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="src",
            config_path=config_path,
            git_enabled=False,
            module_only=True,
            backend=InvalidBackend(),
        )
    )

    assert result.summary.failed == 1
    assert result.modules[0].error_kind == "validation"
    assert destination.read_text(encoding="utf-8") == existing
