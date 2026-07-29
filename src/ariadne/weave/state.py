from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..settings import AriadneConfig
from .documents import ValidationError, validate_document
from .models import GenerationResult, ModuleStatus, PlannedModule
from .planning import module_id


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


def config_fingerprint(config: AriadneConfig) -> str:
    payload = {
        "model": config.model.__dict__,
        "context": config.context.__dict__,
        "generation": config.generation.__dict__,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_entry(
    root: Path,
    plan: PlannedModule,
    previous: dict[str, object] | None,
    config: AriadneConfig,
    fingerprint: str,
    *,
    resume: bool,
) -> dict[str, object]:
    identity = module_id(root, plan)
    output = plan.output.relative_to(root).as_posix()
    entry: dict[str, object] = {
        "module_id": identity,
        "logical_path": plan.module.physical_path,
        "output_path": output,
        "parent_output": (
            plan.parent_output.relative_to(root).as_posix()
            if plan.parent_output is not None
            else None
        ),
        "status": ModuleStatus.PENDING.value,
        "attempts": 0,
        "model": None,
        "error_kind": None,
        "error": None,
        "draft_path": None,
        "error_status_code": None,
        "retryable": None,
        "config_fingerprint": fingerprint,
        "last_attempted_at": None,
        "finished_at": None,
    }
    if (
        resume
        and previous is not None
        and previous.get("config_fingerprint") == fingerprint
        and previous.get("output_path") == output
        and previous.get("status")
        in {ModuleStatus.GENERATED.value, ModuleStatus.UPDATED.value}
        and _valid_existing_output(plan.output, config)
    ):
        entry.update(previous)
    return entry


def update_manifest_entry(
    entry: dict[str, object],
    result: GenerationResult,
    root: Path,
    finished_at: str,
) -> None:
    entry.update(
        {
            "status": result.status,
            "attempts": result.attempts,
            "model": result.model,
            "error_kind": result.error_kind,
            "error": result.error,
            "draft_path": (
                result.draft_path.relative_to(root).as_posix()
                if result.draft_path is not None
                else None
            ),
            "error_status_code": result.error_status_code,
            "retryable": result.retryable,
            "finished_at": finished_at,
        }
    )


def result_from_entry(root: Path, entry: dict[str, object]) -> GenerationResult:
    draft = entry.get("draft_path")
    return GenerationResult(
        str(entry["logical_path"]),
        root / str(entry["output_path"]),
        str(entry["status"]),
        str(entry["model"]) if entry.get("model") is not None else None,
        int(entry.get("attempts", 0)),
        (
            str(entry["error_kind"])
            if entry.get("error_kind") is not None
            else None
        ),
        str(entry["error"]) if entry.get("error") is not None else None,
        root / str(draft) if draft is not None else None,
        (
            int(entry["error_status_code"])
            if entry.get("error_status_code") is not None
            else None
        ),
        (
            bool(entry["retryable"])
            if entry.get("retryable") is not None
            else None
        ),
    )


def _valid_existing_output(path: Path, config: AriadneConfig) -> bool:
    try:
        validate_document(
            path.read_text(encoding="utf-8"),
            require_front_matter=config.generation.include_front_matter,
        )
    except (OSError, UnicodeError, ValidationError):
        return False
    return True


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
