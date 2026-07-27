from pathlib import Path

import pytest

from ariadne.config import ConfigurationError, discover_config, load_config
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

    assert config.root == tmp_path.resolve()
    assert config.file_policy is FilePolicy.TRACKED_AND_UNTRACKED
    assert config.include == ("src/**",)
    assert not config.use_default_ignores
    assert not config.collapse_structural_directories


def test_rejects_unknown_and_invalid_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("repository:\n  mystery: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown repository"):
        load_config(path)

    path.write_text("repository:\n  file_policy: sometimes\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="file_policy"):
        load_config(path)
