from __future__ import annotations

from .models import InspectionResult, LogicalModule


def render_inspection(result: InspectionResult, verbosity: int = 0) -> str:
    if verbosity <= 0:
        lines: list[str] = []
        _render_module(
            result.root_module, lines, prefix="", last=True, verbosity=0
        )
        return "\n".join(lines) + "\n"

    lines = ["Logical modules:"]
    if verbosity >= 2:
        lines[0:0] = [
            f"Repository: {result.context.root}",
            f"Selection:  {_display_path(result.root_module.physical_path)}",
            "",
        ]
    _render_module(
        result.root_module, lines, prefix="", last=True, verbosity=verbosity
    )
    heading = "Ignored paths:" if verbosity >= 2 else "Ignored directories:"
    ignored = (
        result.ignored_paths
        if verbosity >= 2
        else tuple(item for item in result.ignored_paths if item.is_directory)
    )
    lines.extend(["", heading])
    if ignored:
        lines.extend(
            f"  - {_display_path(item.path)} [{item.reason}]"
            for item in ignored
        )
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _render_module(
    module: LogicalModule,
    lines: list[str],
    *,
    prefix: str,
    last: bool,
    verbosity: int,
) -> None:
    connector = "`-- " if last else "|-- "
    details: list[str] = []
    if verbosity >= 1:
        details.append(f"path={_display_path(module.physical_path)}")
    if verbosity >= 2:
        details.extend(
            [
                f"size={_format_bytes(module.source_size)}",
                f"languages={','.join(module.languages) if module.languages else '-'}",
            ]
        )
        if module.collapsed_segments:
            details.append(f"collapsed={'/'.join(module.collapsed_segments)}")
    suffix = f" ({'; '.join(details)})" if details else ""
    lines.append(f"{prefix}{connector}{module.name}{suffix}")
    child_prefix = prefix + ("    " if last else "|   ")
    for index, child in enumerate(module.children):
        _render_module(
            child,
            lines,
            prefix=child_prefix,
            last=index == len(module.children) - 1,
            verbosity=verbosity,
        )


def _display_path(path: str) -> str:
    return path if path != "." else "."


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"
