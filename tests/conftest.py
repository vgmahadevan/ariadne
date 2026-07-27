from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def fixture_repo(tmp_path: Path):
    def copy(name: str) -> Path:
        destination = tmp_path / name
        shutil.copytree(Path(__file__).parent / "fixtures" / name, destination)
        return destination

    return copy


def init_git(root: Path) -> None:
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "tests@example.invalid"],
        ["git", "config", "user.name", "Ariadne Tests"],
        ["git", "add", "."],
        ["git", "commit", "--allow-empty", "-qm", "fixture"],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True)
