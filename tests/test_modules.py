from ariadne.models import PhysicalNode
from ariadne.modules import discover_modules


def _dir(path: str) -> PhysicalNode:
    return PhysicalNode(path, True)


def _source(path: str, size: int = 10, language: str = "Java") -> PhysicalNode:
    return PhysicalNode(path, False, size=size, extension=".java", language=language)


def test_collapses_unary_structural_chain_and_aggregates_metadata() -> None:
    nodes = (
        _dir("."),
        _dir("src"),
        _dir("src/main"),
        _dir("src/main/java"),
        _dir("src/main/java/com"),
        _dir("src/main/java/com/company"),
        _dir("src/main/java/com/company/optimizer"),
        _source("src/main/java/com/company/optimizer/Solver.java", 42),
    )

    root = discover_modules(nodes, ".", collapse=True)

    optimizer = root.children[0]
    assert optimizer.name == "optimizer"
    assert optimizer.physical_path == "src/main/java/com/company/optimizer"
    assert optimizer.collapsed_segments == ("src", "main", "java", "com", "company")
    assert optimizer.languages == ("Java",)
    assert root.source_size == 42


def test_manifest_and_branching_prevent_collapse() -> None:
    nodes = (
        _dir("."),
        _dir("services"),
        _dir("services/api"),
        _dir("services/worker"),
        PhysicalNode("services/package.json", False, is_manifest=True),
        _source("services/api/app.py", language="Python"),
        _source("services/worker/main.go", language="Go"),
    )
    root = discover_modules(nodes, ".", collapse=True)
    services = root.children[0]
    assert services.name == "services"
    assert services.collapsed_segments == ()
    assert [child.name for child in services.children] == ["api", "worker"]


def test_collapse_can_be_disabled() -> None:
    nodes = (_dir("."), _dir("src"), _dir("src/pkg"), _source("src/pkg/a.py", language="Python"))
    root = discover_modules(nodes, ".", collapse=False)
    assert root.children[0].name == "src"
    assert root.children[0].children[0].name == "pkg"
