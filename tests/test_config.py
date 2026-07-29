from pathlib import Path

import pytest

from ariadne.config import (
    ConfigurationError,
    discover_config,
    initialize_config,
    load_config,
)
from ariadne.models import FilePolicy


def test_discovers_and_loads_config_relative_to_repository(tmp_path: Path) -> None:
    config_path = tmp_path / ".ariadne" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
repository:
  root: .
  file_policy: tracked-and-untracked
  include: ["src/**"]
  use_default_ignores: false
modules:
  collapse_structural_directories: false
""",
        encoding="utf-8",
    )
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)

    config = load_config(discover_config(nested))

    assert config.repository.root == tmp_path.resolve()
    assert config.repository.file_policy is FilePolicy.TRACKED_AND_UNTRACKED
    assert config.repository.include == ("src/**",)
    assert not config.repository.use_default_ignores
    assert not config.modules.collapse_structural_directories


def test_rejects_unknown_and_invalid_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("repository:\n  mystery: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown repository"):
        load_config(path)

    path.write_text("repository:\n  file_policy: sometimes\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="file_policy"):
        load_config(path)


def test_loads_phase_two_model_context_and_generation_config(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
model:
  model: local-vllm
  endpoint: http://localhost:9000/v1/
  context_window: 8192
  max_output_tokens: 1024
  timeout_seconds: 10
  headers:
    Authorization: Bearer token
context:
  max_initial_tokens: 6000
  include_generated_docs: false
generation:
  overwrite_generated: false
  max_concurrency: 3
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.model.model == "local-vllm"
    assert config.model.endpoint == "http://localhost:9000/v1"
    assert dict(config.model.headers)["Authorization"] == "Bearer token"
    assert not config.context.include_generated_docs
    assert not config.generation.overwrite_generated
    assert config.generation.max_concurrency == 3


def test_initializes_default_config_and_gitignore_once(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("dist/\n", encoding="utf-8")

    first = initialize_config(tmp_path)
    second = initialize_config(tmp_path)

    assert first == second == tmp_path / ".ariadne" / "config.yaml"
    config = load_config(first)
    assert config.model.endpoint == "http://localhost:8000/v1"
    assert config.generation.max_concurrency == 8
    assert "Update the model name and endpoint" in first.read_text(encoding="utf-8")
    assert gitignore.read_text(encoding="utf-8") == "dist/\n.ariadne/\n"
