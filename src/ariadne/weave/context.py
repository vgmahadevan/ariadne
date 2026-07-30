from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Iterable

from ..discovery.models import InspectionResult, PhysicalNode
from ..llm import ModelRequest
from ..settings import AriadneConfig, ContextConfig
from .models import ContextFile, ModuleContext, PlannedModule

PROMPT_VERSION = 1


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


def _within(path: str, directory: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory + "/")
