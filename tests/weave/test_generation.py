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
    compose_openapi_document,
    persist_document,
    persist_new_test,
    validate_document,
    validate_openapi_document,
    read_document_metadata,
)
from ariadne.weave.models import PlannedModule
from ariadne.weave.planning import plan_modules
from ariadne.weave.tests import plan_test_modules
from ariadne.weave.runner import weave_repository
from ariadne.discovery import inspect_repository
from ariadne.discovery.models import (
    InspectionResult,
    LogicalModule,
    RepositoryContext,
    PhysicalNode,
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
        if len(self.requests) == 1:
            text = "<!-- ariadne-api: true -->\n# HTTP API\n\n`POST /widgets`."
        else:
            text = """openapi: 3.1.0
info:
  title: Widget API
  version: 1.0.0
paths:
  /widgets:
    post:
      operationId: createWidget
      responses:
        '201':
          description: Created
"""
        return ModelResponse(text, "fake-model")


class GeneratedTestsBackend(FakeBackend):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            "from service.routes import create_widget\n\n"
            "def test_create_widget_rejects_missing_payload():\n"
            "    assert create_widget(None) == 400\n",
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
    api_path = tmp_path / "service" / "service-genai-openapi.yaml"
    assert api_result.successful[0].output_path == api_path
    assert api_path.is_file()
    api_provenance = read_document_metadata(api_path)["ariadne"]
    assert api_provenance["document_type"] == "openapi"
    assert "complete, valid OpenAPI 3.1" in backend.requests[1].user_prompt
    validate_openapi_document(api_path.read_text(encoding="utf-8"))


def test_api_planning_selects_topmost_connected_boundaries(tmp_path: Path) -> None:
    nested = LogicalModule("routes", "service/routes")
    service = LogicalModule("service", "service", children=(nested,))
    other = LogicalModule("other", "other")
    root = LogicalModule("repository", ".", children=(service, other))
    inspection = InspectionResult(
        RepositoryContext(tmp_path, tmp_path, None, False), (), (), root
    )
    config = AriadneConfig()
    for plan in plan_modules(inspection, config, module_only=False):
        if plan.module.physical_path in {"service", "service/routes", "other"}:
            plan.output.parent.mkdir(parents=True, exist_ok=True)
            plan.output.write_text(
                compose_document(
                    "# API\n", config=config, module=plan.module,
                    generated_at=datetime.now(timezone.utc), source_commit_value=None,
                    model="fake", has_api=True,
                ),
                encoding="utf-8",
            )

    api_plans = plan_modules(inspection, config, module_only=False, api=True)

    assert [plan.module.physical_path for plan in api_plans] == ["service", "other"]
    assert all(plan.output.name.endswith("-genai-openapi.yaml") for plan in api_plans)


def test_openapi_validation_rejects_operations_without_responses() -> None:
    document = compose_openapi_document(
        "openapi: 3.1.0\ninfo:\n  title: API\n  version: 1.0.0\npaths:\n  /items:\n    get: {}\n",
        module=LogicalModule("api", "api"),
        generated_at=datetime.now(timezone.utc),
        source_commit_value=None,
        model="fake",
    )
    with pytest.raises(ValidationError, match="requires responses"):
        validate_openapi_document(document)


def test_test_planning_uses_existing_framework_directories_across_languages(
    tmp_path: Path,
) -> None:
    python_module = LogicalModule("service", "src/service", languages=("Python",))
    java_module = LogicalModule(
        "widgets", "src/main/java/com/acme/widgets", languages=("Java",)
    )
    root = LogicalModule("repository", ".", children=(python_module, java_module))
    inspection = InspectionResult(
        RepositoryContext(tmp_path, tmp_path, None, False),
        (
            PhysicalNode("src/service/main.py", False, language="Python"),
            PhysicalNode("tests/test_existing.py", False, language="Python"),
            PhysicalNode(
                "src/main/java/com/acme/widgets/Widget.java", False,
                language="Java",
            ),
            PhysicalNode(
                "src/test/java/com/acme/widgets/WidgetTest.java", False,
                language="Java",
            ),
        ),
        (),
        root,
    )

    plans = plan_test_modules(
        inspection, plan_modules(inspection, AriadneConfig(), module_only=False)
    )

    assert [plan.output.relative_to(tmp_path).as_posix() for plan in plans] == [
        "tests/test_service_genai.py",
        "src/test/java/com/acme/widgets/WidgetsGenaiTest.java",
    ]


@pytest.mark.parametrize(
    ("language", "source_name", "expected"),
    [
        ("TypeScript", "index.ts", "tests/component.genai.test.ts"),
        ("Go", "main.go", "component/component_genai_test.go"),
        ("Rust", "lib.rs", "tests/component_genai.rs"),
        ("Ruby", "component.rb", "spec/component_genai_spec.rb"),
        ("C#", "Component.cs", "tests/ComponentGenaiTests.cs"),
        ("Swift", "Component.swift", "Tests/ComponentGenaiTests.swift"),
        ("Shell", "component.sh", "tests/component_genai_test.bats"),
    ],
)
def test_test_planning_has_language_specific_fallbacks(
    tmp_path: Path, language: str, source_name: str, expected: str
) -> None:
    module = LogicalModule("component", "component", languages=(language,))
    inspection = InspectionResult(
        RepositoryContext(tmp_path, tmp_path, None, False),
        (PhysicalNode(f"component/{source_name}", False, language=language),),
        (),
        module,
    )

    plans = plan_test_modules(
        inspection, plan_modules(inspection, AriadneConfig(), module_only=True)
    )

    assert plans[0].output.relative_to(tmp_path).as_posix() == expected


def test_test_weave_creates_new_file_without_running_or_overwriting(
    tmp_path: Path,
) -> None:
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "routes.py").write_text(
        "def create_widget(payload):\n    return 400 if payload is None else 201\n",
        encoding="utf-8",
    )
    config_path = _config(tmp_path)
    backend = GeneratedTestsBackend()

    result = asyncio.run(
        weave_repository(
            cwd=tmp_path, path="service", config_path=config_path,
            git_enabled=False, module_only=True, tests=True, backend=backend,
        )
    )

    destination = tmp_path / "tests" / "test_service_genai.py"
    assert result.successful[0].output_path == destination
    assert destination.read_text(encoding="utf-8").startswith("from service.routes")
    assert "existing test framework" in backend.requests[0].user_prompt

    original = destination.read_text(encoding="utf-8")
    resumed = asyncio.run(
        weave_repository(
            cwd=tmp_path, path="service", config_path=config_path,
            git_enabled=False, module_only=True, tests=True, resume=True,
            backend=backend,
        )
    )
    assert len(resumed.successful) == 1
    assert len(backend.requests) == 1

    repeated = asyncio.run(
        weave_repository(
            cwd=tmp_path, path="service", config_path=config_path,
            git_enabled=False, module_only=True, tests=True, backend=backend,
        )
    )
    assert repeated.summary.partial == 1
    assert "refusing to overwrite" in (repeated.modules[0].error or "")
    assert destination.read_text(encoding="utf-8") == original


def test_new_test_persistence_rejects_existing_files(tmp_path: Path) -> None:
    destination = tmp_path / "test_module_genai.py"
    destination.write_text("# human file\n", encoding="utf-8")

    with pytest.raises(PersistenceError, match="overwrite"):
        persist_new_test(destination, "def test_generated(): pass")

    assert destination.read_text(encoding="utf-8") == "# human file\n"


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
