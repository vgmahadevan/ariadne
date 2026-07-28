from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

import yaml

from .config import discover_config, initialize_config, load_config
from .inspection import inspect_repository
from .llm import LLMBackend, ModelError, ModelRequest, OpenAICompatibleBackend
from .models import (
    AriadneConfig,
    ContextConfig,
    FilePolicy,
    InspectionResult,
    LogicalModule,
    PhysicalNode,
)


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
    model: str


def weave_repository(
    *,
    cwd: Path | None = None,
    path: str | None = None,
    config_path: Path | None = None,
    root: str | None = None,
    git_enabled: bool = True,
    file_policy: FilePolicy | None = None,
    module_only: bool = False,
    force: bool = False,
    backend: LLMBackend | None = None,
    now: Callable[[], datetime] | None = None,
    on_config_created: Callable[[Path], None] | None = None,
    on_progress: Callable[[int, int, LogicalModule], None] | None = None,
) -> tuple[GenerationResult, ...]:
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
    plans = plan_modules(inspection, config, module_only=module_only)
    _check_collisions(plans)
    if on_progress is not None:
        on_progress(0, len(plans), plans[0].module)
    results: list[GenerationResult] = []
    generated: dict[str, Path] = {}
    clock = now or (lambda: datetime.now().astimezone())
    commit = source_commit(inspection)
    for index, plan in enumerate(plans, start=1):
        parent_output = generated.get(_parent_module_path(plan.module.physical_path))
        effective_plan = PlannedModule(
            plan.module,
            plan.ancestors,
            parent_output or plan.parent_output,
            plan.output,
        )
        context = assemble_context(
            inspection,
            effective_plan,
            config,
            source_commit_value=commit,
        )
        try:
            response = selected_backend.generate(build_prompt(context))
        except ModelError as exc:
            raise ModelError(
                exc.kind,
                f"{exc} Model settings are in {selected_config}. "
                "Verify that endpoint points to a running OpenAI-compatible "
                "chat-completions service and that model names and headers are correct.",
            ) from exc
        generated_at = clock()
        document = compose_document(
            response.text,
            config=config,
            module=plan.module,
            generated_at=generated_at,
            source_commit_value=commit,
            model=response.model,
        )
        validate_document(document, require_front_matter=config.generation.include_front_matter)
        persist_document(plan.output, document, config=config, force=force)
        if not plan.output.is_file():
            raise PersistenceError(
                f"documentation output does not exist after persistence: {plan.output}"
            )
        generated[plan.module.physical_path] = plan.output
        results.append(
            GenerationResult(plan.module.physical_path, plan.output, response.model)
        )
        if on_progress is not None:
            on_progress(index, len(plans), plan.module)
    return tuple(results)


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
    return ModuleContext(
        repository_name=inspection.context.root.name,
        repository_root=str(inspection.context.root),
        source_commit=source_commit_value,
        module=module,
        ancestors=plan.ancestors,
        tree=tree,
        files=files,
        omissions=omissions,
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
            "- ONLY IF USEFUL: Describe parent, child, and external relationships if established.",
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
            "You have A LOT of freedom with what sections to use, including ad hoc sections.",
            "For Areas for Improvement, surface obvious problems like unintentionlly duplicated code, or critical issues that need attention. But be concise and don't just dump TODOs."
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
        metadata = _existing_metadata(destination)
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
        if plan.parent_output and plan.parent_output.is_file():
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


def _existing_metadata(path: Path) -> dict[str, object]:
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


def _parent_module_path(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "." if parent == "." else parent


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
