from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from ..discovery.models import InspectionResult, PhysicalNode
from .models import PlannedModule
from .planning import slug


_TEST_PARTS = {"test", "tests", "spec", "specs", "__tests__"}
_LANGUAGE_EXTENSIONS = {
    "Python": ".py",
    "JavaScript": ".js",
    "TypeScript": ".ts",
    "Java": ".java",
    "Go": ".go",
    "Rust": ".rs",
    "C": ".c",
    "C++": ".cpp",
    "C#": ".cs",
    "Ruby": ".rb",
    "PHP": ".php",
    "Swift": ".swift",
    "Kotlin": ".kt",
    "Scala": ".scala",
    "Shell": ".bats",
}


def plan_test_modules(
    inspection: InspectionResult,
    plans: tuple[PlannedModule, ...],
) -> tuple[PlannedModule, ...]:
    """Select source-bearing modules and assign deterministic new test files."""
    selected: list[PlannedModule] = []
    for plan in plans:
        for language in _direct_languages(inspection, plan):
            destination = _test_destination(inspection, plan, language)
            selected.append(
                PlannedModule(plan.module, plan.ancestors, None, destination)
            )
    return tuple(selected)


def test_language(inspection: InspectionResult, plan: PlannedModule) -> str:
    matches = [
        language
        for language in _direct_languages(inspection, plan)
        if plan.output.suffix.casefold() == _LANGUAGE_EXTENSIONS[language]
    ]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"cannot determine test language for: {plan.output}")


def is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = {part.casefold() for part in pure.parts}
    name = pure.name.casefold()
    return bool(parts & _TEST_PARTS) or bool(
        re.search(r"(^test_|_test\.|\.test\.|\.spec\.|tests?\.)", name)
    )


def _direct_languages(
    inspection: InspectionResult, plan: PlannedModule
) -> tuple[str, ...]:
    languages: set[str] = set()
    module_path = plan.module.physical_path
    for node in inspection.physical_nodes:
        if (
            node.is_directory
            or node.language is None
            or is_test_path(node.path)
            or PurePosixPath(node.path).parent.as_posix() != module_path
        ):
            continue
        languages.add(node.language)
    return tuple(sorted(languages))


def _test_destination(
    inspection: InspectionResult,
    plan: PlannedModule,
    language: str,
) -> Path:
    root = inspection.context.root
    existing = [
        node
        for node in inspection.physical_nodes
        if not node.is_directory
        and node.language == language
        and is_test_path(node.path)
        and not {"fixture", "fixtures"}
        & {part.casefold() for part in PurePosixPath(node.path).parts}
    ]
    directory = _existing_test_directory(root, existing, plan)
    module_slug = slug(plan.module.name).replace("-", "_")
    extension = _LANGUAGE_EXTENSIONS[language]

    if language == "Go":
        directory = root if plan.module.physical_path == "." else root / plan.module.physical_path
        filename = f"{module_slug}_genai_test.go"
    elif language == "Java":
        directory = directory or _jvm_test_directory(root, plan, "java")
        filename = f"{_class_name(plan.module.name)}GenaiTest.java"
    elif language == "Kotlin":
        directory = directory or _jvm_test_directory(root, plan, "kotlin")
        filename = f"{_class_name(plan.module.name)}GenaiTest.kt"
    elif language == "Scala":
        directory = directory or _jvm_test_directory(root, plan, "scala")
        filename = f"{_class_name(plan.module.name)}GenaiSpec.scala"
    elif language == "Python":
        directory = directory or root / "tests"
        filename = f"test_{module_slug}_genai.py"
    elif language in {"JavaScript", "TypeScript"}:
        directory = directory or root / "tests"
        style = "spec" if any(".spec." in PurePosixPath(n.path).name for n in existing) else "test"
        filename = f"{module_slug}.genai.{style}{extension}"
    elif language == "Ruby":
        directory = directory or root / "spec"
        filename = f"{module_slug}_genai_spec.rb"
    elif language == "PHP":
        directory = directory or root / "tests"
        filename = f"{_class_name(plan.module.name)}GenaiTest.php"
    elif language == "Rust":
        directory = directory or root / "tests"
        filename = f"{module_slug}_genai.rs"
    elif language == "C#":
        directory = directory or root / "tests"
        filename = f"{_class_name(plan.module.name)}GenaiTests.cs"
    elif language == "Swift":
        directory = directory or root / "Tests"
        filename = f"{_class_name(plan.module.name)}GenaiTests.swift"
    else:
        directory = directory or root / "tests"
        filename = f"{module_slug}_genai_test{extension}"
    return directory / filename


def _existing_test_directory(
    root: Path,
    nodes: list[PhysicalNode],
    plan: PlannedModule,
) -> Path | None:
    if not nodes:
        return None
    module_parts = set(PurePosixPath(plan.module.physical_path).parts)
    ranked = sorted(
        nodes,
        key=lambda node: (
            -len(module_parts & set(PurePosixPath(node.path).parts)),
            node.path,
        ),
    )
    return root / PurePosixPath(ranked[0].path).parent


def _jvm_test_directory(root: Path, plan: PlannedModule, language: str) -> Path:
    path = PurePosixPath(plan.module.physical_path)
    parts = list(path.parts)
    if "main" in parts:
        parts[parts.index("main")] = "test"
        return root.joinpath(*parts)
    return root / "src" / "test" / language


def _class_name(value: str) -> str:
    return "".join(part.capitalize() for part in re.findall(r"[A-Za-z0-9]+", value)) or "Module"
