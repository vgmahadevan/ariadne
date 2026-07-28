from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable, Iterable

import yaml

from .config import discover_config, initialize_config, load_config
from .inspection import inspect_repository
from .llm import (
    LLMBackend,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    OpenAICompatibleBackend,
)
from .models import (
    AriadneConfig,
    ContextConfig,
    FilePolicy,
    InspectionResult,
    LogicalModule,
    PhysicalNode,
)
from .state import RunStateStore


class GenerationError(RuntimeError):
    pass


class ValidationError(GenerationError):
    pass


class PersistenceError(GenerationError):
    pass


@dataclass(frozen=True)
class ContextFile:
    path: str
    content: str
    evidence: str
    truncated: bool = False


@dataclass(frozen=True)
class ModuleContext:
    repository_name: str
    repository_root: str
    source_commit: str | None
    module: LogicalModule
    ancestors: tuple[str, ...]
    tree: tuple[str, ...]
    files: tuple[ContextFile, ...]
    omissions: tuple[str, ...]
    missing_parent: bool = False


@dataclass(frozen=True)
class PlannedModule:
    module: LogicalModule
    ancestors: tuple[str, ...]
    parent_output: Path | None
    output: Path


@dataclass(frozen=True)
class GenerationResult:
    module_path: str
    output_path: Path
    status: str
    model: str | None = None
    attempts: int = 0
    error_kind: str | None = None
    error: str | None = None
    draft_path: Path | None = None
    error_status_code: int | None = None
    retryable: bool | None = None


class ModuleStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    GENERATED = "generated"
    UPDATED = "updated"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WeaveSummary:
    generated: int = 0
    updated: int = 0
    failed: int = 0
    partial: int = 0
    cancelled: int = 0


@dataclass(frozen=True)
class ProgressEvent:
    index: int
    total: int
    module: LogicalModule
    status: str
    attempt: int = 0
    error_kind: str | None = None


@dataclass(frozen=True)
class WeaveResult:
    run_id: str
    modules: tuple[GenerationResult, ...]
    summary: WeaveSummary
    manifest_path: Path
    interrupted: bool = False

    @property
    def successful(self) -> tuple[GenerationResult, ...]:
        return tuple(
            item
            for item in self.modules
            if item.status in {ModuleStatus.GENERATED.value, ModuleStatus.UPDATED.value}
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
    force: bool = False,
    resume: bool = False,
    max_concurrency: int | None = None,
    backend: LLMBackend | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_config_created: Callable[[Path], None] | None = None,
    on_progress: Callable[[ProgressEvent], None] | None = None,
) -> WeaveResult:
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
    selected_backend = backend or OpenAICompatibleBackend(config.model)
    owns_backend = backend is None
    plans = plan_modules(inspection, config, module_only=module_only)
    _check_collisions(plans)
    clock = now or (lambda: datetime.now().astimezone())
    commit = source_commit(inspection)
    concurrency = (
        config.generation.max_concurrency
        if max_concurrency is None
        else max_concurrency
    )
    if concurrency <= 0:
        raise GenerationError("max concurrency must be a positive integer")
    store = RunStateStore(inspection.context.root)
    config_fingerprint = _config_fingerprint(config)
    selection = inspection.context.selection.relative_to(
        inspection.context.root
    ).as_posix() or "."
    previous = store.load_latest() if resume else None
    if previous is not None and (
        previous.get("repository_root") != str(inspection.context.root)
        or previous.get("selection") != selection
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
        _manifest_entry(
            inspection.context.root,
            plan,
            prior_modules.get(_module_id(inspection.context.root, plan)),
            config,
            config_fingerprint,
            resume=resume,
        )
        for plan in plans
    ]
    current_ids = {str(entry["module_id"]) for entry in entries}
    removed_modules = [
        item
        for item in (previous or {}).get("modules", [])
        if isinstance(item, dict) and item.get("module_id") not in current_ids
    ]
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "repository_root": str(inspection.context.root),
        "selection": selection,
        "source_commit": commit,
        "tool_version": _tool_version(),
        "model": config.model.model,
        "config_fingerprint": config_fingerprint,
        "started_at": (previous or {}).get("started_at", started.isoformat()),
        "finished_at": None,
        "interrupted": False,
        "summary": {},
        "modules": entries,
        "removed_modules": removed_modules,
    }
    manifest_path = store.save(manifest)
    results: list[GenerationResult | None] = [None] * len(plans)
    parent_indices = _parent_indices(plans)
    completed: set[int] = set()
    for index, entry in enumerate(entries):
        if entry["status"] in {
            ModuleStatus.GENERATED.value,
            ModuleStatus.UPDATED.value,
        }:
            completed.add(index)
            results[index] = _result_from_entry(inspection.context.root, entry)
    running: dict[asyncio.Task[GenerationResult], int] = {}
    report_buffer: dict[int, GenerationResult] = {}
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

    def record(index: int, result: GenerationResult) -> None:
        nonlocal next_report
        results[index] = result
        completed.add(index)
        _update_manifest_entry(
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
        parent = parent_indices[index]
        missing_parent = (
            parent is not None
            and results[parent] is not None
            and results[parent].status
            not in {ModuleStatus.GENERATED.value, ModuleStatus.UPDATED.value}
        )
        return await _execute_module(
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
        )

    interrupted = False
    try:
        while len(completed) < len(plans):
            for index in range(len(plans)):
                if len(running) >= concurrency:
                    break
                if index in completed or index in running.values():
                    continue
                parent = parent_indices[index]
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
        interrupted = True
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
    summary = _summarize(final_results)
    manifest["summary"] = summary.__dict__
    manifest["finished_at"] = clock().isoformat()
    manifest["interrupted"] = interrupted
    manifest_path = store.save(manifest)
    return WeaveResult(run_id, final_results, summary, manifest_path, interrupted)


async def _execute_module(
    inspection: InspectionResult,
    plan: PlannedModule,
    config: AriadneConfig,
    backend: LLMBackend,
    commit: str | None,
    *,
    force: bool,
    missing_parent: bool,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    run_id: str,
    on_attempt: Callable[[int], None],
) -> GenerationResult:
    existed = plan.output.is_file()
    response_text: str | None = None
    response_model: str | None = None
    attempts = 0
    reduce_context = False
    for attempt in (1, 2):
        attempts = attempt
        on_attempt(attempt)
        attempt_config = config
        if attempt == 2 and reduce_context:
            attempt_config = replace(
                config,
                context=replace(
                    config.context,
                    max_initial_tokens=max(
                        1, config.context.max_initial_tokens // 2
                    ),
                    include_generated_docs=False,
                ),
            )
        try:
            context = assemble_context(
                inspection,
                plan,
                attempt_config,
                source_commit_value=commit,
                missing_parent=missing_parent,
            )
            response = await backend.generate(build_prompt(context))
            response_text = response.text
            response_model = response.model
            document = compose_document(
                response.text,
                config=config,
                module=plan.module,
                generated_at=clock(),
                source_commit_value=commit,
                model=response.model,
            )
            validate_document(
                document,
                require_front_matter=config.generation.include_front_matter,
            )
            persist_document(plan.output, document, config=config, force=force)
            if not plan.output.is_file():
                raise PersistenceError(
                    "documentation output does not exist after persistence: "
                    f"{plan.output}"
                )
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                (
                    ModuleStatus.UPDATED.value
                    if existed
                    else ModuleStatus.GENERATED.value
                ),
                response.model,
                attempts,
            )
        except asyncio.CancelledError:
            raise
        except ModelError as exc:
            if attempt == 1 and exc.retryable:
                reduce_context = exc.kind is ModelErrorKind.CONTEXT_LENGTH
                delay = exc.retry_after if exc.retry_after is not None else 1.0
                await sleep(min(30.0, delay))
                continue
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                ModuleStatus.FAILED.value,
                response_model,
                attempts,
                exc.kind.value,
                str(exc),
                None,
                exc.status_code,
                exc.retryable,
            )
        except (ValidationError, PersistenceError, OSError) as exc:
            draft_path = None
            if response_text is not None and _markdown_like(response_text):
                try:
                    draft_path = _persist_partial_draft(
                        inspection.context.root,
                        run_id,
                        plan,
                        response_text,
                        response_model or config.model.model,
                        clock(),
                    )
                except PersistenceError as draft_exc:
                    exc = draft_exc
            kind = (
                "validation"
                if isinstance(exc, ValidationError)
                else "persistence"
            )
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                (
                    ModuleStatus.PARTIAL.value
                    if draft_path is not None
                    else ModuleStatus.FAILED.value
                ),
                response_model,
                attempts,
                kind,
                str(exc),
                draft_path,
            )
        except Exception as exc:
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                ModuleStatus.FAILED.value,
                response_model,
                attempts,
                "internal",
                f"{type(exc).__name__}: {exc}",
            )
    raise AssertionError("module attempts exhausted")


def _manifest_entry(
    root: Path,
    plan: PlannedModule,
    previous: dict[str, object] | None,
    config: AriadneConfig,
    config_fingerprint: str,
    *,
    resume: bool,
) -> dict[str, object]:
    module_id = _module_id(root, plan)
    output = plan.output.relative_to(root).as_posix()
    entry: dict[str, object] = {
        "module_id": module_id,
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
        "config_fingerprint": config_fingerprint,
        "last_attempted_at": None,
        "finished_at": None,
    }
    if (
        resume
        and previous is not None
        and previous.get("config_fingerprint") == config_fingerprint
        and previous.get("output_path") == output
        and previous.get("status")
        in {ModuleStatus.GENERATED.value, ModuleStatus.UPDATED.value}
        and _valid_existing_output(plan.output, config)
    ):
        entry.update(previous)
    return entry


def _update_manifest_entry(
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


def _result_from_entry(root: Path, entry: dict[str, object]) -> GenerationResult:
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


def _parent_indices(plans: tuple[PlannedModule, ...]) -> tuple[int | None, ...]:
    by_output = {plan.output: index for index, plan in enumerate(plans)}
    return tuple(
        by_output.get(plan.parent_output) if plan.parent_output is not None else None
        for plan in plans
    )


def _module_id(root: Path, plan: PlannedModule) -> str:
    identity = (
        f"{plan.module.physical_path}\0"
        f"{plan.output.relative_to(root).as_posix()}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _config_fingerprint(config: AriadneConfig) -> str:
    payload = {
        "model": config.model.__dict__,
        "context": config.context.__dict__,
        "generation": config.generation.__dict__,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_existing_output(path: Path, config: AriadneConfig) -> bool:
    try:
        validate_document(
            path.read_text(encoding="utf-8"),
            require_front_matter=config.generation.include_front_matter,
        )
    except (OSError, UnicodeError, ValidationError):
        return False
    return True


def _markdown_like(text: str) -> bool:
    return bool(re.search(r"^# \S.*$", text, flags=re.MULTILINE))


def _persist_partial_draft(
    root: Path,
    run_id: str,
    plan: PlannedModule,
    text: str,
    model: str,
    generated_at: datetime,
) -> Path:
    destination = (
        root
        / ".ariadne"
        / "drafts"
        / run_id
        / (
            f"{_slug(plan.module.physical_path)}-"
            f"{_module_id(root, plan)[:8]}.md"
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "ariadne": {
            "generated": True,
            "draft": True,
            "generated_at": generated_at.isoformat(),
            "tool_version": _tool_version(),
            "model": model,
            "logical_module": plan.module.physical_path,
            "status": "PARTIAL",
            "human_reviewed": False,
            "human_modified": False,
        }
    }
    document = (
        "---\n"
        f"{yaml.safe_dump(metadata, sort_keys=False).strip()}\n"
        "---\n\n"
        f"{text.strip()}\n"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, destination)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise PersistenceError(f"cannot persist partial draft: {destination}") from exc
    return destination


def _summarize(results: tuple[GenerationResult, ...]) -> WeaveSummary:
    counts = {
        status.value: sum(item.status == status.value for item in results)
        for status in ModuleStatus
    }
    return WeaveSummary(
        generated=counts[ModuleStatus.GENERATED.value],
        updated=counts[ModuleStatus.UPDATED.value],
        failed=counts[ModuleStatus.FAILED.value],
        partial=counts[ModuleStatus.PARTIAL.value],
        cancelled=counts[ModuleStatus.CANCELLED.value],
    )


def top_down_modules(
    root: LogicalModule,
    *,
    module_only: bool = False,
) -> tuple[tuple[LogicalModule, tuple[str, ...]], ...]:
    result: list[tuple[LogicalModule, tuple[str, ...]]] = []

    def visit(module: LogicalModule, ancestors: tuple[str, ...]) -> None:
        result.append((module, ancestors))
        if not module_only:
            for child in module.children:
                visit(child, (*ancestors, module.name))

    visit(root, ())
    return tuple(result)


def output_path(root: Path, module: LogicalModule, suffix: str) -> Path:
    directory = root if module.physical_path == "." else root / module.physical_path
    return directory / f"{_slug(module.name)}{suffix}"


def plan_modules(
    inspection: InspectionResult,
    config: AriadneConfig,
    *,
    module_only: bool,
) -> tuple[PlannedModule, ...]:
    entries: list[
        tuple[LogicalModule, tuple[str, ...], LogicalModule | None, Path]
    ] = []

    def visit(
        module: LogicalModule,
        ancestors: tuple[str, ...],
        parent: LogicalModule | None,
    ) -> None:
        destination = output_path(
            inspection.context.root, module, config.generation.output_suffix
        )
        entries.append((module, ancestors, parent, destination))
        if not module_only:
            for child in module.children:
                visit(child, (*ancestors, module.name), module)

    visit(inspection.root_module, (), None)
    counts: dict[Path, int] = {}
    for _, _, _, destination in entries:
        counts[destination] = counts.get(destination, 0) + 1
    destinations: dict[int, Path] = {}
    for module, ancestors, _, destination in entries:
        if counts[destination] > 1:
            identity = "/".join((*ancestors, module.name, module.physical_path))
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
            destination = destination.with_name(
                f"{_slug(module.name)}-{digest}{config.generation.output_suffix}"
            )
        destinations[id(module)] = destination
    plans = tuple(
        PlannedModule(
            module,
            ancestors,
            destinations.get(id(parent)) if parent is not None else None,
            destinations[id(module)],
        )
        for module, ancestors, parent, _ in entries
    )
    _check_collisions(plans)
    return plans


def assemble_context(
    inspection: InspectionResult,
    plan: PlannedModule,
    config: AriadneConfig,
    *,
    source_commit_value: str | None = None,
    missing_parent: bool = False,
) -> ModuleContext:
    module = plan.module
    nodes = [
        node
        for node in inspection.physical_nodes
        if _within(node.path, module.physical_path)
    ]
    tree = _render_tree(nodes, module.physical_path, config.context.max_tree_depth)
    candidates = _context_candidates(
        inspection.context.root, nodes, plan, config
    )
    files, omissions = _read_bounded_files(candidates, config.context, config)
    if missing_parent:
        omissions = (
            "- newly generated parent documentation unavailable because the "
            "parent attempt failed",
            *omissions,
        )
    return ModuleContext(
        repository_name=inspection.context.root.name,
        repository_root=str(inspection.context.root),
        source_commit=source_commit_value,
        module=module,
        ancestors=plan.ancestors,
        tree=tree,
        files=files,
        omissions=omissions,
        missing_parent=missing_parent,
    )


def build_prompt(context: ModuleContext) -> ModelRequest:
    system = (
        "You are Ariadne, a disciplined technical writer documenting one logical "
        "module. Ground claims in the supplied primary evidence. Human documentation "
        "is secondary evidence. Prior AI-generated documentation is unverified, may "
        "be stale, and must never override source evidence. Do not repeat unsupported "
        "claims. Acknowledge uncertainty. Return only final Markdown beginning with "
        "one level-one title; do not include YAML front matter or a generation disclaimer."
    )
    lines = [
        "# Documentation task",
        f"Repository: {context.repository_name}",
        f"Repository root: {context.repository_root}",
        f"Source commit: {context.source_commit or 'unavailable'}",
        f"Logical module: {context.module.name}",
        f"Module location: {context.module.physical_path}",
        f"Ancestors: {' / '.join(context.ancestors) or '(repository selection root)'}",
        f"Languages: {', '.join(context.module.languages) or 'unknown'}",
        f"Child modules: {', '.join(child.name for child in context.module.children) or 'none'}",
        "",
        "# Directory structure",
        *context.tree,
        "",
        "# Evidence",
    ]
    for item in context.files:
        marker = " (truncated)" if item.truncated else ""
        lines.extend(
            [
                f"## {item.path} [{item.evidence}{marker}]",
                "```text",
                item.content,
                "```",
            ]
        )
    if context.omissions:
        lines.extend(["", "# Context omissions", *context.omissions])
    leaf_guidance = []
    if not context.module.children:
        leaf_guidance = [
            "- This is a leaf module. Consider giving additional detail about "
            "the sibling files located together here: explain how they divide "
            "responsibilities, collaborate, and form the local implementation. "
            "Do this only where the supplied evidence supports useful detail, "
            "and do not fall back to file-by-file paraphrase."
        ]
    lines.extend(
        [
            "",
            "# Generation instructions",
            "- Explain the module's summary, responsibilities, operation, and organization.",
            "- Reference concrete files and symbols when supported by evidence.",
            "- Describe parent, child, and external relationships only when useful and established by evidence.",
            "- Avoid file-by-file paraphrase and omit irrelevant sections.",
            "- State uncertainties instead of inventing intent.",
            "- Use repository-relative Markdown links.",
            "- Surface important implementation details; e.g., describe and explain calculations in that module's domain",
            "- Highlight assumptions made, unexpected findings, and surprising things that would be useful to a first time reader of the module",
            "- Similarly, do not spend much time on facts that are implicitly obvious (e.g., a function called 'add_two_numbers()' adds two numbers)",
            *leaf_guidance,
            "",
            "# Documentation contract",
            "Use a flexible selection of: Summary; Purpose and Responsibilities; "
            "How It Works; Architecture and Organization; Important Files and APIs; "
            "Data Flow; Dependencies and Relationships; Configuration and External "
            "Interfaces; Uncertainties and Review Notes; Areas for Improvement.",
            "Use whichever sections fit the evidence; useful ad hoc sections are allowed.",
            "Include Areas for Improvement only for concrete, significant problems "
            "such as unintended duplication or critical issues. Be concise and do "
            "not turn it into a TODO inventory.",
        ]
    )
    return ModelRequest(system, "\n".join(lines))


def compose_document(
    draft: str,
    *,
    config: AriadneConfig,
    module: LogicalModule,
    generated_at: datetime,
    source_commit_value: str | None,
    model: str,
) -> str:
    body = draft.strip()
    timestamp = generated_at.isoformat()
    commit_text = (
        f" from source commit `{source_commit_value}`" if source_commit_value else ""
    )
    disclaimer = (
        f"*This AI-generated documentation was generated by Ariadne on "
        f"{generated_at.date().isoformat()}{commit_text}. If a human maintainer "
        "modifies or reviews this document, please record that change in the "
        "provenance metadata or review notes.*"
    )
    parts: list[str] = []
    if config.generation.include_front_matter:
        metadata = {
            "ariadne": {
                "generated": True,
                "generated_at": timestamp,
                "tool_version": _tool_version(),
                "model": model,
                "source_commit": source_commit_value,
                "logical_module": module.physical_path,
                "status": "AI-GENERATED",
                "human_reviewed": False,
                "human_modified": False,
            }
        }
        yaml_text = yaml.safe_dump(metadata, sort_keys=False).strip()
        parts.append(f"---\n{yaml_text}\n---")
    parts.extend([disclaimer, body])
    return "\n\n".join(parts) + "\n"


def validate_document(document: str, *, require_front_matter: bool = True) -> None:
    if not document.strip():
        raise ValidationError("generated document is empty")
    remainder = document
    metadata: dict[str, object] = {}
    if document.startswith("---\n"):
        end = document.find("\n---\n", 4)
        if end < 0:
            raise ValidationError("front matter is not terminated")
        try:
            parsed = yaml.safe_load(document[4:end]) or {}
        except yaml.YAMLError as exc:
            raise ValidationError("front matter is invalid YAML") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("front matter must be a mapping")
        metadata = parsed
        remainder = document[end + 5 :]
    elif require_front_matter:
        raise ValidationError("front matter is missing")
    if require_front_matter:
        provenance = metadata.get("ariadne")
        if not isinstance(provenance, dict) or not all(
            key in provenance
            for key in {
                "generated", "generated_at", "tool_version", "model",
                "logical_module", "status", "human_reviewed",
            }
        ):
            raise ValidationError("front matter provenance is incomplete")
    if "AI-generated documentation" not in remainder:
        raise ValidationError("AI-generated disclaimer is missing")
    titles = re.findall(r"^# (.+)$", remainder, flags=re.MULTILINE)
    if len(titles) != 1 or not titles[0].strip():
        raise ValidationError("document must contain exactly one level-one title")


def persist_document(
    destination: Path,
    document: str,
    *,
    config: AriadneConfig,
    force: bool = False,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        metadata = read_document_metadata(destination)
        provenance = metadata.get("ariadne", {})
        if not isinstance(provenance, dict):
            provenance = {}
        human_changed = bool(
            provenance.get("human_reviewed") or provenance.get("human_modified")
        )
        generated = provenance.get("generated") is True
        if human_changed and not (force or config.generation.overwrite_human_modified):
            raise PersistenceError(f"refusing to overwrite human-modified document: {destination}")
        if not generated and not force:
            raise PersistenceError(f"refusing to overwrite non-Ariadne document: {destination}")
        if generated and not config.generation.overwrite_generated and not force:
            raise PersistenceError(f"generated document already exists: {destination}")
    if not config.generation.atomic_writes:
        destination.write_text(document, encoding="utf-8")
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, destination)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise PersistenceError(f"cannot persist documentation: {destination}") from exc


def source_commit(inspection: InspectionResult) -> str | None:
    if not inspection.context.git_available:
        return None
    result = subprocess.run(
        [
            "git", "-c", "safe.directory=*", "-C",
            str(inspection.context.root), "rev-parse", "HEAD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _context_candidates(
    root: Path,
    nodes: list[PhysicalNode],
    plan: PlannedModule,
    config: AriadneConfig,
) -> list[tuple[int, str, Path, str]]:
    candidates: list[tuple[int, str, Path, str]] = []
    for node in nodes:
        if node.is_directory or node.path.endswith(config.generation.output_suffix):
            continue
        candidates.append(
            (_file_priority(node), node.path, root / node.path, _evidence(node))
        )
    if config.context.include_generated_docs:
        generated_paths: set[Path] = set()
        if (
            config.context.include_parent_docs
            and plan.parent_output
            and plan.parent_output.is_file()
        ):
            generated_paths.add(plan.parent_output)
        if plan.output.is_file():
            generated_paths.add(plan.output)
        if config.context.include_parent_docs:
            current = plan.output.parent.parent
            while current == root or root in current.parents:
                for item in current.glob(f"*{config.generation.output_suffix}"):
                    if item.is_file():
                        generated_paths.add(item)
                if current == root:
                    break
                current = current.parent
        for path in generated_paths:
            rel = path.relative_to(root).as_posix()
            candidates.append((6, rel, path, "prior AI-generated documentation; unverified"))
    return sorted(candidates, key=lambda item: (item[0], item[1]))


def _read_bounded_files(
    candidates: list[tuple[int, str, Path, str]],
    context_config: ContextConfig,
    config: AriadneConfig,
) -> tuple[tuple[ContextFile, ...], tuple[str, ...]]:
    max_tokens = min(
        context_config.max_initial_tokens,
        max(1, config.model.context_window - config.model.max_output_tokens),
    )
    character_budget = int(max_tokens * context_config.characters_per_token)
    prompt_reserve = min(6000, character_budget // 4)
    remaining = max(0, character_budget - prompt_reserve)
    selected: list[ContextFile] = []
    omissions: list[str] = []
    for _, rel, path, evidence in candidates:
        try:
            raw = path.read_bytes()
        except OSError:
            omissions.append(f"- {rel}: unreadable")
            continue
        if b"\0" in raw[:8192]:
            omissions.append(f"- {rel}: binary content omitted")
            continue
        truncated = len(raw) > context_config.max_file_bytes
        raw = raw[: context_config.max_file_bytes]
        text = raw.decode("utf-8", errors="replace")
        if len(text) > remaining:
            if remaining < 200:
                omissions.append(f"- {rel}: context budget exhausted")
                continue
            text = text[:remaining] + "\n[truncated by context budget]"
            truncated = True
        selected.append(ContextFile(rel, text, evidence, truncated))
        remaining -= len(text)
    return tuple(selected), tuple(omissions)


def _render_tree(
    nodes: Iterable[PhysicalNode],
    module_path: str,
    max_depth: int,
) -> tuple[str, ...]:
    base_parts = () if module_path == "." else PurePosixPath(module_path).parts
    result: list[str] = []
    for node in nodes:
        parts = PurePosixPath(node.path).parts
        relative = parts[len(base_parts) :]
        if len(relative) <= max_depth + 1:
            result.append(f"- {node.path}{'/' if node.is_directory else ''}")
    return tuple(result)


def _file_priority(node: PhysicalNode) -> int:
    name = PurePosixPath(node.path).name.lower()
    parts = {part.lower() for part in PurePosixPath(node.path).parts}
    if node.is_manifest or name.startswith(".") or name in {"makefile", "dockerfile"}:
        return 0
    if name.startswith(("main.", "index.", "__init__.", "api.", "interface.")):
        return 1
    if parts & {"test", "tests", "spec", "specs"}:
        return 4
    if node.language:
        return 2
    if node.is_documentation:
        return 3
    return 5


def _evidence(node: PhysicalNode) -> str:
    if node.is_documentation:
        return "human-authored documentation; secondary evidence"
    return "source/configuration; primary evidence"


def read_document_metadata(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return {}
        end = text.find("\n---\n", 4)
        parsed = yaml.safe_load(text[4:end]) if end >= 0 else {}
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}


def _within(path: str, directory: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory + "/")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "module"


def _check_collisions(plans: tuple[PlannedModule, ...]) -> None:
    seen: dict[Path, str] = {}
    for plan in plans:
        previous = seen.get(plan.output)
        if previous is not None:
            raise GenerationError(
                f"documentation output collision: {previous} and "
                f"{plan.module.physical_path} resolve to {plan.output}"
            )
        seen[plan.output] = plan.module.physical_path


def _tool_version() -> str:
    try:
        return version("ariadne")
    except PackageNotFoundError:
        return "0.1.0"
