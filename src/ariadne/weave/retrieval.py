from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from ..discovery.models import InspectionResult, LogicalModule, PhysicalNode
from ..llm import ToolCall, ToolDefinition
from ..settings import RetrievalConfig


class RetrievalError(ValueError):
    pass


@dataclass(frozen=True)
class RetrievalSummary:
    requested: int = 0
    executed: int = 0
    errors: int = 0
    per_tool: tuple[tuple[str, int], ...] = ()
    warnings: tuple[str, ...] = ()
    termination_reason: str | None = None


TOOL_DEFINITIONS = {
    "list_directory": ToolDefinition(
        "list_directory",
        "List the immediate admissible children of a repository directory.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    "read_file": ToolDefinition(
        "read_file",
        "Read bounded text from an admissible repository file.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    "search_code": ToolDefinition(
        "search_code",
        "Search admissible repository text using a literal or regular expression.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "regex": {"type": "boolean"},
                "path": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1},
                "context_lines": {"type": "integer", "minimum": 0},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "get_module_tree": ToolDefinition(
        "get_module_tree",
        "Return bounded logical-module and physical structure beneath a path.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_depth": {"type": "integer", "minimum": 0},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
}


class RetrievalHarness:
    def __init__(
        self,
        inspection: InspectionResult,
        config: RetrievalConfig,
    ) -> None:
        self.inspection = inspection
        self.config = config
        self.nodes = {node.path: node for node in inspection.physical_nodes}
        self.requested = 0
        self.executed = 0
        self.errors = 0
        self.counts: Counter[str] = Counter()
        self.identical: Counter[str] = Counter()
        self.warnings: list[str] = []
        self.termination_reason: str | None = None

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(TOOL_DEFINITIONS[name] for name in self.config.tools)

    async def execute(self, call: ToolCall) -> str:
        self.requested += 1
        if call.name not in self.config.tools:
            return self._error(call.name, "unknown-tool", "tool is not enabled")
        key = json.dumps(
            [call.name, call.arguments], sort_keys=True, separators=(",", ":")
        )
        self.identical[key] += 1
        if self.identical[key] > self.config.max_identical_calls:
            self.termination_reason = "identical-call-limit"
            self.warnings.append("identical-call-limit")
            return self._error(
                call.name,
                "identical-call-limit",
                "equivalent call limit reached; finish with available evidence",
            )
        if self.identical[key] == self.config.max_identical_calls:
            self.warnings.append("identical-call-warning")
        if self.executed >= self.config.max_tool_calls_per_module:
            self.termination_reason = "tool-call-budget"
            self.warnings.append("tool-call-budget")
            return self._error(
                call.name,
                "tool-call-budget",
                "tool call budget exhausted; finish with available evidence",
            )
        self.executed += 1
        self.counts[call.name] += 1
        try:
            value = await asyncio.wait_for(
                asyncio.to_thread(self._execute_sync, call),
                timeout=self.config.tool_timeout_seconds,
            )
            return self._bounded({"ok": True, "tool": call.name, "result": value})
        except asyncio.TimeoutError:
            return self._error(call.name, "timeout", "tool call timed out")
        except (RetrievalError, OSError, UnicodeError, re.error) as exc:
            return self._error(call.name, "tool-error", str(exc))

    def summary(self) -> RetrievalSummary:
        return RetrievalSummary(
            requested=self.requested,
            executed=self.executed,
            errors=self.errors,
            per_tool=tuple(sorted(self.counts.items())),
            warnings=tuple(dict.fromkeys(self.warnings)),
            termination_reason=self.termination_reason,
        )

    def _execute_sync(self, call: ToolCall) -> object:
        functions: dict[str, Callable[[dict[str, object]], object]] = {
            "list_directory": self._list_directory,
            "read_file": self._read_file,
            "search_code": self._search_code,
            "get_module_tree": self._get_module_tree,
        }
        return functions[call.name](call.arguments)

    def _list_directory(self, arguments: dict[str, object]) -> object:
        path = self._path(arguments.get("path"), require_directory=True)
        prefix = "" if path == "." else path + "/"
        children = []
        for node in self.nodes.values():
            if node.path == path or not node.path.startswith(prefix):
                continue
            remainder = node.path[len(prefix):]
            if "/" in remainder:
                continue
            children.append(_node_record(node, self.inspection.root_module))
        return {"path": path, "children": sorted(children, key=lambda x: x["path"])}

    def _read_file(self, arguments: dict[str, object]) -> object:
        path = self._path(arguments.get("path"), require_directory=False)
        start = _optional_int(arguments, "start_line", minimum=1) or 1
        end = _optional_int(arguments, "end_line", minimum=1)
        if end is not None and end < start:
            raise RetrievalError("end_line must be greater than or equal to start_line")
        raw = (self.inspection.context.root / path).read_bytes()
        if b"\0" in raw[:8192]:
            raise RetrievalError("binary files cannot be read")
        lines = raw.decode("utf-8").splitlines()
        selected = lines[start - 1:end]
        return {
            "path": path,
            "start_line": start,
            "end_line": min(len(lines), end or len(lines)),
            "total_lines": len(lines),
            "content": "\n".join(selected),
        }

    def _search_code(self, arguments: dict[str, object]) -> object:
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            raise RetrievalError("query must be a nonempty string")
        regex = arguments.get("regex", False)
        if not isinstance(regex, bool):
            raise RetrievalError("regex must be a boolean")
        base = self._path(arguments.get("path", "."), require_directory=None)
        limit = min(_optional_int(arguments, "max_results", minimum=1) or 50, 200)
        context = min(_optional_int(arguments, "context_lines", minimum=0) or 0, 5)
        pattern = re.compile(query if regex else re.escape(query))
        matches = []
        for node in sorted(self.nodes.values(), key=lambda item: item.path):
            if node.is_directory or not _within(node.path, base):
                continue
            raw = (self.inspection.context.root / node.path).read_bytes()
            if b"\0" in raw[:8192]:
                continue
            lines = raw.decode("utf-8").splitlines()
            for index, line in enumerate(lines):
                if not pattern.search(line):
                    continue
                first = max(0, index - context)
                last = min(len(lines), index + context + 1)
                matches.append(
                    {
                        "path": node.path,
                        "line": index + 1,
                        "context_start": first + 1,
                        "text": "\n".join(lines[first:last]),
                    }
                )
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def _get_module_tree(self, arguments: dict[str, object]) -> object:
        path = self._path(arguments.get("path"), require_directory=None)
        depth = min(_optional_int(arguments, "max_depth", minimum=0) or 3, 10)
        base_parts = () if path == "." else PurePosixPath(path).parts
        physical = []
        for node in sorted(self.nodes.values(), key=lambda item: item.path):
            if not _within(node.path, path):
                continue
            relative = PurePosixPath(node.path).parts[len(base_parts):]
            if len(relative) <= depth + 1:
                physical.append(_node_record(node, self.inspection.root_module))
        logical = [
            {
                "name": module.name,
                "path": module.physical_path,
                "languages": list(module.languages),
            }
            for module in _modules(self.inspection.root_module)
            if _within(module.physical_path, path)
            and _relative_depth(module.physical_path, path) <= depth
        ]
        return {"path": path, "logical_modules": logical, "physical": physical}

    def _path(
        self, value: object, *, require_directory: bool | None
    ) -> str:
        if not isinstance(value, str) or not value:
            raise RetrievalError("path must be a nonempty repository-relative path")
        if "\\" in value:
            raise RetrievalError("path must use repository-relative POSIX syntax")
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts:
            raise RetrievalError("path escapes the configured repository")
        path = "." if value in {".", "./"} else pure.as_posix().rstrip("/")
        node = self.nodes.get(path)
        if node is None:
            raise RetrievalError("path is not admitted by repository policy")
        if require_directory is True and not node.is_directory:
            raise RetrievalError("path is not a directory")
        if require_directory is False and node.is_directory:
            raise RetrievalError("path is not a file")
        return path

    def _error(self, tool: str, code: str, message: str) -> str:
        self.errors += 1
        return self._bounded(
            {"ok": False, "tool": tool, "error": {"code": code, "message": message}}
        )

    def _bounded(self, value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
        raw = encoded.encode("utf-8")
        if len(raw) <= self.config.max_result_bytes:
            return encoded
        excerpt = raw[: self.config.max_result_bytes - 160].decode(
            "utf-8", errors="ignore"
        )
        while True:
            bounded = json.dumps(
                {
                    "ok": True,
                    "truncated": True,
                    "result_excerpt": excerpt,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            if len(bounded.encode("utf-8")) <= self.config.max_result_bytes:
                return bounded
            excerpt = excerpt[:-16]


def _optional_int(
    arguments: dict[str, object], name: str, *, minimum: int
) -> int | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RetrievalError(f"{name} must be an integer >= {minimum}")
    return value


def _node_record(
    node: PhysicalNode, root_module: LogicalModule
) -> dict[str, object]:
    return {
        "path": node.path,
        "type": "directory" if node.is_directory else "file",
        "size": node.size,
        "language": node.language,
        "manifest": node.is_manifest,
        "documentation": node.is_documentation,
        "logical_module": any(
            item.physical_path == node.path for item in _modules(root_module)
        ),
    }


def _modules(root: LogicalModule):
    yield root
    for child in root.children:
        yield from _modules(child)


def _within(path: str, directory: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory + "/")


def _relative_depth(path: str, base: str) -> int:
    path_parts = () if path == "." else PurePosixPath(path).parts
    base_parts = () if base == "." else PurePosixPath(base).parts
    return max(0, len(path_parts) - len(base_parts))
