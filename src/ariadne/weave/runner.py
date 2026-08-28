from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from ..config import (
    discover_config,
    initialize_config,
    initialize_internal_readme,
    load_config,
)
from ..discovery import inspect_repository
from ..discovery.repository import source_commit
from ..llm import LLMBackend, OpenAICompatibleBackend
from ..settings import FilePolicy
from .documents import tool_version
from .context import PROMPT_VERSION
from .executor import execute_module
from .models import (
    GenerationError,
    GenerationResult,
    ModuleStatus,
    PlannedModule,
    ProgressEvent,
    WeaveResult,
    WeaveSummary,
)
from .planning import module_id, parent_indices, plan_modules
from .tests import plan_test_modules, test_language
from .state import (
    RunStateStore,
    manifest_entry,
    resume_fingerprint,
    result_from_entry,
    update_manifest_entry,
)


async def weave_repository(
    *,
    cwd: Path | None = None,
    path: str | None = None,
    config_path: Path | None = None,
    root: str | None = None,
    git_enabled: bool = True,
    file_policy: FilePolicy | None = None,
    module_only: bool = False,
    api: bool = False,
    tests: bool = False,
    force: bool = False,
    resume: bool = False,
    max_concurrency: int | None = None,
    backend: LLMBackend | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_config_created: Callable[[Path], None] | None = None,
    on_progress: Callable[[ProgressEvent], None] | None = None,
) -> WeaveResult:
    if api and tests:
        raise GenerationError("--api and --tests cannot be used together")
    cwd = (cwd or Path.cwd()).resolve()
    config_start = Path(root).resolve() if root else cwd
    selected_config = (
        config_path.resolve() if config_path else discover_config(config_start)
    )
    if selected_config is None:
        initial_inspection = inspect_repository(
            cwd=cwd,
            path=path,
            root=root,
            git_enabled=git_enabled,
            file_policy=file_policy,
        )
        selected_config = initialize_config(initial_inspection.context.root)
        if on_config_created is not None:
            on_config_created(selected_config)
    config = load_config(selected_config)
    inspection = inspect_repository(
        cwd=cwd,
        path=path,
        config_path=selected_config,
        root=root,
        git_enabled=git_enabled,
        file_policy=file_policy,
    )
    retrieval_inspection = None
    if config.retrieval.enabled:
        if inspection.context.selection == inspection.context.root:
            retrieval_inspection = inspection
        else:
            retrieval_inspection = inspect_repository(
                cwd=inspection.context.root,
                path=".",
                config_path=selected_config,
                root=str(inspection.context.root),
                git_enabled=git_enabled,
                file_policy=file_policy,
            )
    initialize_internal_readme(inspection.context.root)
    selected_backend = backend or OpenAICompatibleBackend(config.model)
    owns_backend = backend is None
    plans = plan_modules(inspection, config, module_only=module_only, api=api)
    if tests:
        plans = plan_test_modules(inspection, plans)
    concurrency = (
        config.generation.max_concurrency
        if max_concurrency is None
        else max_concurrency
    )
    if concurrency <= 0:
        raise GenerationError("max concurrency must be a positive integer")
    if on_progress is not None and plans:
        on_progress(
            ProgressEvent(
                0,
                len(plans),
                plans[0].module,
                ModuleStatus.PENDING.value,
            )
        )

    clock = now or (lambda: datetime.now().astimezone())
    commit = source_commit(inspection)
    store = RunStateStore(inspection.context.root)
    fingerprint = resume_fingerprint(config)
    document_type = "test" if tests else "openapi" if api else "module"
    fingerprint = f"{fingerprint}:{document_type}"
    selection = inspection.context.selection.relative_to(
        inspection.context.root
    ).as_posix() or "."
    previous = store.load_latest() if resume else None
    if previous is not None and (
        previous.get("repository_root") != str(inspection.context.root)
        or previous.get("selection") != selection
        or previous.get("document_type", "module") != document_type
    ):
        raise GenerationError(
            "--resume found no compatible latest run for this repository selection"
        )
    started = clock()
    run_id = (
        str(previous["run_id"])
        if previous is not None
        else f"{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    prior_modules = {
        item.get("module_id"): item
        for item in (previous or {}).get("modules", [])
        if isinstance(item, dict) and isinstance(item.get("module_id"), str)
    }
    entries = [
        manifest_entry(
            inspection.context.root,
            plan,
            prior_modules.get(module_id(inspection.context.root, plan)),
            config,
            fingerprint,
            resume=resume,
        )
        for plan in plans
    ]
    current_ids = {str(entry["module_id"]) for entry in entries}
    manifest: dict[str, object] = {
        "schema_version": 2,
        "run_id": run_id,
        "repository_root": str(inspection.context.root),
        "selection": selection,
        "source_commit": commit,
        "tool_version": tool_version(),
        "model": config.model.model,
        "config_fingerprint": fingerprint,
        "prompt_version": PROMPT_VERSION,
        "document_type": document_type,
        "started_at": (previous or {}).get("started_at", started.isoformat()),
        "finished_at": None,
        "interrupted": False,
        "summary": {},
        "modules": entries,
        "removed_modules": [
            item
            for item in (previous or {}).get("modules", [])
            if isinstance(item, dict) and item.get("module_id") not in current_ids
        ],
    }
    manifest_path = store.save(manifest)
    results: list[GenerationResult | None] = [None] * len(plans)
    completed: set[int] = set()
    for index, entry in enumerate(entries):
        if entry["status"] in {
            ModuleStatus.GENERATED.value,
            ModuleStatus.UPDATED.value,
        }:
            completed.add(index)
            results[index] = result_from_entry(inspection.context.root, entry)

    parents = parent_indices(plans)
    running: dict[asyncio.Task[GenerationResult], int] = {}
    report_buffer: dict[int, GenerationResult] = {}
    next_report = _report_preserved(
        plans, results, completed, on_progress
    )

    def record(index: int, result: GenerationResult) -> None:
        nonlocal next_report
        results[index] = result
        completed.add(index)
        update_manifest_entry(
            entries[index], result, inspection.context.root, clock().isoformat()
        )
        store.save(manifest)
        report_buffer[index] = result
        while next_report in report_buffer:
            reported = report_buffer.pop(next_report)
            if on_progress is not None:
                on_progress(
                    ProgressEvent(
                        next_report + 1,
                        len(plans),
                        plans[next_report].module,
                        reported.status,
                        reported.attempts,
                        reported.error_kind,
                    )
                )
            next_report += 1

    async def launch(index: int) -> GenerationResult:
        entries[index]["status"] = ModuleStatus.RUNNING.value
        entries[index]["last_attempted_at"] = clock().isoformat()
        store.save(manifest)
        parent = parents[index]
        missing_parent = (
            parent is not None
            and results[parent] is not None
            and results[parent].status
            not in {ModuleStatus.GENERATED.value, ModuleStatus.UPDATED.value}
        )
        return await execute_module(
            inspection,
            plans[index],
            config,
            selected_backend,
            commit,
            force=force,
            missing_parent=missing_parent,
            clock=clock,
            sleep=sleep,
            run_id=run_id,
            on_attempt=lambda attempt: _record_attempt(
                entries[index], attempt, manifest, store, clock
            ),
            retrieval_inspection=retrieval_inspection,
            api=api,
            tests=tests,
            test_language=(test_language(inspection, plans[index]) if tests else None),
        )

    try:
        while len(completed) < len(plans):
            for index in range(len(plans)):
                if len(running) >= concurrency:
                    break
                if index in completed or index in running.values():
                    continue
                parent = parents[index]
                if parent is None or parent in completed:
                    running[asyncio.create_task(launch(index))] = index
            if not running:
                raise GenerationError("module scheduler could not make progress")
            done, _ = await asyncio.wait(
                running, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                index = running.pop(task)
                record(index, task.result())
    except asyncio.CancelledError:
        for task, index in running.items():
            task.cancel()
            entries[index]["status"] = ModuleStatus.PENDING.value
        await asyncio.gather(*running, return_exceptions=True)
        manifest["interrupted"] = True
        store.save(manifest)
        raise
    finally:
        if owns_backend and isinstance(selected_backend, OpenAICompatibleBackend):
            await selected_backend.aclose()

    final_results = tuple(item for item in results if item is not None)
    summary = summarize(final_results)
    manifest["summary"] = summary.__dict__
    manifest["finished_at"] = clock().isoformat()
    manifest_path = store.save(manifest)
    return WeaveResult(run_id, final_results, summary, manifest_path)


def summarize(results: tuple[GenerationResult, ...]) -> WeaveSummary:
    return WeaveSummary(
        generated=sum(item.status == ModuleStatus.GENERATED.value for item in results),
        updated=sum(item.status == ModuleStatus.UPDATED.value for item in results),
        failed=sum(item.status == ModuleStatus.FAILED.value for item in results),
        partial=sum(item.status == ModuleStatus.PARTIAL.value for item in results),
    )


def _report_preserved(
    plans: tuple[PlannedModule, ...],
    results: list[GenerationResult | None],
    completed: set[int],
    on_progress: Callable[[ProgressEvent], None] | None,
) -> int:
    next_report = 0
    while next_report in completed:
        preserved = results[next_report]
        if preserved is not None and on_progress is not None:
            on_progress(
                ProgressEvent(
                    next_report + 1,
                    len(plans),
                    plans[next_report].module,
                    preserved.status,
                    preserved.attempts,
                    preserved.error_kind,
                )
            )
        next_report += 1
    return next_report


def _record_attempt(
    entry: dict[str, object],
    attempt: int,
    manifest: dict[str, object],
    store: RunStateStore,
    clock: Callable[[], datetime],
) -> None:
    entry["attempts"] = attempt
    entry["last_attempted_at"] = clock().isoformat()
    store.save(manifest)
