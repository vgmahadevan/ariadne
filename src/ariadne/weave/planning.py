from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..discovery.models import InspectionResult, LogicalModule
from ..settings import AriadneConfig
from .models import GenerationError, PlannedModule
OPENAPI_OUTPUT_SUFFIX = "-genai-openapi.yaml"


def plan_modules(
    inspection: InspectionResult,
    config: AriadneConfig,
    *,
    module_only: bool,
    api: bool = False,
) -> tuple[PlannedModule, ...]:
    entries: list[
        tuple[LogicalModule, tuple[str, ...], LogicalModule | None, Path]
    ] = []

    def visit(
        module: LogicalModule,
        ancestors: tuple[str, ...],
        parent: LogicalModule | None,
    ) -> None:
        destination = _output_path(
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
                f"{slug(module.name)}-{digest}{config.generation.output_suffix}"
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
    if api:
        from .documents import read_document_metadata

        marked_outputs: set[Path] = set()
        for plan in plans:
            provenance = read_document_metadata(plan.output).get("ariadne")
            if isinstance(provenance, dict) and provenance.get("api") is True:
                marked_outputs.add(plan.output)
        selected: list[PlannedModule] = []
        for plan in plans:
            if plan.output not in marked_outputs:
                continue
            # A connected group of API-marked modules describes one API. Generate
            # its specification at the highest marked boundary, not at every level.
            if plan.parent_output in marked_outputs:
                continue
            selected.append(
                PlannedModule(
                    plan.module,
                    plan.ancestors,
                    None,
                    plan.output.with_name(
                        f"{plan.output.name[:-len(config.generation.output_suffix)]}"
                        f"{OPENAPI_OUTPUT_SUFFIX}"
                    ),
                )
            )
        plans = tuple(selected)
    _check_collisions(plans)
    return plans


def parent_indices(plans: tuple[PlannedModule, ...]) -> tuple[int | None, ...]:
    by_output = {plan.output: index for index, plan in enumerate(plans)}
    return tuple(
        by_output.get(plan.parent_output) if plan.parent_output is not None else None
        for plan in plans
    )


def module_id(root: Path, plan: PlannedModule) -> str:
    identity = (
        f"{plan.module.physical_path}\0"
        f"{plan.output.relative_to(root).as_posix()}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized or "module"


def _output_path(root: Path, module: LogicalModule, suffix: str) -> Path:
    directory = root if module.physical_path == "." else root / module.physical_path
    return directory / f"{slug(module.name)}{suffix}"


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
