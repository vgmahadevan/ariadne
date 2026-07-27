from __future__ import annotations

from .models import InspectionResult, LogicalModule


def render_inspection(result: InspectionResult) -> str:
    lines = [
        f"Repository: {result.context.root}",
        f"Selection:  {_display_path(result.root_module.physical_path)}",
        "",
        "Logical modules:",
    ]
    _render_module(result.root_module, lines, prefix="", last=True)
    lines.extend(["", "Ignored paths:"])
    if result.ignored_paths:
        lines.extend(
            f"  - {_display_path(item.path)} [{item.reason}]"
            for item in result.ignored_paths
        )
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _render_module(
    module: LogicalModule, lines: list[str], *, prefix: str, last: bool
) -> None:
    connector = "`-- " if last else "|-- "
    details = [
        f"path={_display_path(module.physical_path)}",
        f"size={_format_bytes(module.source_size)}",
        f"languages={','.join(module.languages) if module.languages else '-'}",
    ]
    if module.collapsed_segments:
        details.append(f"collapsed={'/'.join(module.collapsed_segments)}")
    lines.append(f"{prefix}{connector}{module.name} ({'; '.join(details)})")
    child_prefix = prefix + ("    " if last else "|   ")
    for index, child in enumerate(module.children):
        _render_module(
            child,
            lines,
            prefix=child_prefix,
            last=index == len(module.children) - 1,
        )


def _display_path(path: str) -> str:
    return path if path != "." else "."


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"
