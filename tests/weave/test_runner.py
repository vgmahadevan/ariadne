"""Retry, resume, concurrency, and cancellation coverage."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ariadne.llm import ModelError, ModelErrorKind, ModelRequest, ModelResponse
from ariadne.settings import AriadneConfig
from ariadne.weave import GenerationError, ModuleStatus, weave_repository
from ariadne.weave.state import resume_fingerprint


def _config(root: Path, *, concurrency: int = 1) -> Path:
    path = root / ".ariadne" / "config.yaml"
    path.parent.mkdir()
    path.write_text(
        f"""
repository:
  file_policy: all-nonignored
model:
  model: fake
  endpoint: http://unused/v1
generation:
  max_concurrency: {concurrency}
""",
        encoding="utf-8",
    )
    return path


def _repository(root: Path) -> None:
    (root / "services" / "api").mkdir(parents=True)
    (root / "services" / "worker").mkdir(parents=True)
    (root / "services" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "services" / "api" / "main.py").write_text(
        "API = True\n", encoding="utf-8"
    )
    (root / "services" / "worker" / "main.py").write_text(
        "WORKER = True\n", encoding="utf-8"
    )


def _module_path(request: ModelRequest) -> str:
    marker = "Module location: "
    return request.user_prompt.split(marker, 1)[1].splitlines()[0]


class ConcurrentFailureBackend:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.calls: list[tuple[str, str]] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        module = _module_path(request)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.calls.append((module, request.user_prompt))
        try:
            await asyncio.sleep(0.01)
            if module == "services":
                raise ModelError(ModelErrorKind.AUTHENTICATION, "denied")
            return ModelResponse(f"# {module}\n\nGenerated.", "fake")
        finally:
            self.active -= 1


def test_parent_failure_releases_children_with_bounded_parallelism(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    backend = ConcurrentFailureBackend()
    result = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="services",
            config_path=_config(tmp_path, concurrency=2),
            git_enabled=False,
            backend=backend,
        )
    )

    assert [item.module_path for item in result.modules] == [
        "services",
        "services/api",
        "services/worker",
    ]
    assert result.modules[0].status == ModuleStatus.FAILED.value
    assert result.summary.generated == 2
    assert result.summary.failed == 1
    assert backend.maximum == 2
    child_prompts = [prompt for module, prompt in backend.calls if module != "services"]
    assert len(child_prompts) == 2
    assert all("parent attempt failed" in prompt for prompt in child_prompts)


class PartialBackend:
    async def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse("# First\n\n# Second\n\nIncomplete.", "fake")


def test_invalid_markdown_like_output_is_saved_as_partial(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    result = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="src",
            config_path=_config(tmp_path),
            git_enabled=False,
            module_only=True,
            backend=PartialBackend(),
        )
    )

    module = result.modules[0]
    assert module.status == ModuleStatus.PARTIAL.value
    assert module.draft_path is not None and module.draft_path.is_file()
    assert not module.output_path.exists()
    draft = module.draft_path.read_text(encoding="utf-8")
    assert "draft: true" in draft
    assert "logical_module: src" in draft


class OverflowThenSuccessBackend:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.user_prompt)
        if len(self.prompts) == 1:
            raise ModelError(
                ModelErrorKind.CONTEXT_LENGTH,
                "too much context",
                status_code=400,
            )
        return ModelResponse("# src\n\nRecovered.", "fake")


def test_context_overflow_retry_removes_generated_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = _config(tmp_path)
    asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="src",
            config_path=config,
            git_enabled=False,
            module_only=True,
            backend=SelectiveBackend(),
        )
    )
    backend = OverflowThenSuccessBackend()

    async def no_sleep(delay: float) -> None:
        pass

    result = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="src",
            config_path=config,
            git_enabled=False,
            module_only=True,
            backend=backend,
            sleep=no_sleep,
        )
    )

    assert result.summary.updated == 1
    assert result.modules[0].attempts == 2
    assert "prior AI-generated documentation; unverified" in backend.prompts[0]
    assert "prior AI-generated documentation; unverified" not in backend.prompts[1]


class SelectiveBackend:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[str] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        module = _module_path(request)
        self.calls.append(module)
        if module in self.fail:
            raise ModelError(ModelErrorKind.AUTHENTICATION, "denied")
        return ModelResponse(f"# {module}\n\nGenerated.", "fake")


def test_resume_reuses_successes_and_retries_failed_modules(tmp_path: Path) -> None:
    _repository(tmp_path)
    config = _config(tmp_path)
    first_backend = SelectiveBackend({"services/api"})
    first = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="services",
            config_path=config,
            git_enabled=False,
            backend=first_backend,
        )
    )
    assert first.summary.failed == 1

    second_backend = SelectiveBackend()
    resumed = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="services",
            config_path=config,
            git_enabled=False,
            backend=second_backend,
            resume=True,
        )
    )

    assert resumed.run_id == first.run_id
    assert second_backend.calls == ["services/api"]
    assert resumed.summary.failed == 0
    assert all(item.output_path.is_file() for item in resumed.successful)
    manifest = json.loads(resumed.manifest_path.read_text(encoding="utf-8"))
    assert manifest["finished_at"]
    assert manifest["summary"]["failed"] == 0


class BlockingBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_cancellation_restores_running_modules_to_pending(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = _config(tmp_path)

    async def cancel_weave() -> None:
        backend = BlockingBackend()
        task = asyncio.create_task(
            weave_repository(
                cwd=tmp_path,
                path="src",
                config_path=config,
                git_enabled=False,
                module_only=True,
                backend=backend,
            )
        )
        await backend.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_weave())
    index = json.loads(
        (tmp_path / ".ariadne" / "state.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (
            tmp_path
            / ".ariadne"
            / "runs"
            / f"{index['latest_run_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["interrupted"] is True
    assert manifest["modules"][0]["status"] == ModuleStatus.PENDING.value
    assert manifest["modules"][0]["attempts"] == 1


def test_explicit_zero_concurrency_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(GenerationError, match="positive integer"):
        asyncio.run(
            weave_repository(
                cwd=tmp_path,
                path="src",
                config_path=_config(tmp_path),
                git_enabled=False,
                backend=SelectiveBackend(),
                max_concurrency=0,
            )
        )


def test_operational_settings_do_not_invalidate_resume_fingerprint() -> None:
    config = AriadneConfig()
    changed = replace(
        config,
        generation=replace(
            config.generation,
            max_concurrency=1,
            overwrite_generated=False,
            overwrite_human_modified=True,
        ),
        model=replace(config.model, timeout_seconds=1),
    )

    assert resume_fingerprint(changed) == resume_fingerprint(config)
