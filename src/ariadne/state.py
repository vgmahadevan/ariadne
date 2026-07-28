from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StateError(RuntimeError):
    pass


class RunStateStore:
    def __init__(self, repository_root: Path) -> None:
        self.root = repository_root / ".ariadne"
        self.runs = self.root / "runs"
        self.index = self.root / "state.json"

    def save(self, manifest: dict[str, Any]) -> Path:
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise StateError("run manifest has no run ID")
        destination = self.runs / f"{run_id}.json"
        _write_json(destination, manifest)
        _write_json(self.index, {"latest_run_id": run_id})
        return destination

    def load_latest(self) -> dict[str, Any] | None:
        index = _read_json(self.index)
        if index is None:
            return None
        run_id = index.get("latest_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise StateError(f"invalid run-state index: {self.index}")
        manifest = _read_json(self.runs / f"{run_id}.json")
        if manifest is None:
            raise StateError(f"run manifest referenced by state is missing: {run_id}")
        modules = manifest.get("modules")
        if (
            not isinstance(modules, list)
            or manifest.get("run_id") != run_id
            or not isinstance(manifest.get("repository_root"), str)
            or not isinstance(manifest.get("selection"), str)
        ):
            raise StateError(f"invalid run manifest: {run_id}")
        for module in modules:
            if isinstance(module, dict) and module.get("status") == "running":
                module["status"] = "pending"
        return manifest


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read run state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"run state must be a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise StateError(f"cannot persist run state {path}: {exc}") from exc
