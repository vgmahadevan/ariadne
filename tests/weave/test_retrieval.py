from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ariadne.discovery import inspect_repository
from ariadne.llm import ModelRequest, ModelResponse, ToolCall
from ariadne.settings import FilePolicy, RetrievalConfig
from ariadne.weave import weave_repository
from ariadne.weave.retrieval import RetrievalHarness


def _full_inspection(root: Path):
    return inspect_repository(
        cwd=root,
        path=".",
        git_enabled=False,
        file_policy=FilePolicy.ALL_NONIGNORED,
    )


def test_retrieval_tools_are_repository_bounded_and_policy_filtered(
    tmp_path: Path,
) -> None:
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "shared").mkdir()
    (tmp_path / "services" / "api" / "main.py").write_text(
        "from shared.contract import VALUE\n", encoding="utf-8"
    )
    (tmp_path / "shared" / "contract.py").write_text(
        "VALUE = 42\n", encoding="utf-8"
    )
    (tmp_path / ".ariadne").mkdir()
    (tmp_path / ".ariadne" / "secret.txt").write_text(
        "secret\n", encoding="utf-8"
    )
    harness = RetrievalHarness(_full_inspection(tmp_path), RetrievalConfig())

    search = json.loads(
        asyncio.run(
            harness.execute(
                ToolCall("1", "search_code", {"query": "VALUE = 42"})
            )
        )
    )
    read = json.loads(
        asyncio.run(
            harness.execute(
                ToolCall("2", "read_file", {"path": "shared/contract.py"})
            )
        )
    )
    ranged = json.loads(
        asyncio.run(
            harness.execute(
                ToolCall(
                    "range",
                    "read_file",
                    {
                        "path": "services/api/main.py",
                        "start_line": 1,
                        "end_line": 1,
                    },
                )
            )
        )
    )
    listing = json.loads(
        asyncio.run(
            harness.execute(
                ToolCall("list", "list_directory", {"path": "shared"})
            )
        )
    )
    tree = json.loads(
        asyncio.run(
            harness.execute(
                ToolCall(
                    "tree",
                    "get_module_tree",
                    {"path": "services", "max_depth": 2},
                )
            )
        )
    )
    denied = json.loads(
        asyncio.run(
            harness.execute(
                ToolCall("3", "read_file", {"path": ".ariadne/secret.txt"})
            )
        )
    )

    assert search["result"]["matches"][0]["path"] == "shared/contract.py"
    assert read["result"]["content"] == "VALUE = 42"
    assert ranged["result"]["start_line"] == 1
    assert "shared.contract" in ranged["result"]["content"]
    assert listing["result"]["children"][0]["path"] == "shared/contract.py"
    assert any(
        item["path"] == "services/api/main.py"
        for item in tree["result"]["physical"]
    )
    assert denied["ok"] is False
    assert denied["error"]["code"] == "tool-error"


class CrossRepositoryBackend:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        tool_messages = [
            message for message in request.messages if message.role == "tool"
        ]
        if not tool_messages:
            return ModelResponse(
                None,
                "fake",
                (
                    ToolCall(
                        "search-1",
                        "search_code",
                        {"query": "SHARED_CONTRACT"},
                    ),
                ),
            )
        result = json.loads(tool_messages[-1].content or "{}")
        assert result["result"]["matches"][0]["path"] == "shared/contract.py"
        return ModelResponse("# API\n\nUses the shared contract.", "fake")


def test_subtree_weave_can_retrieve_from_elsewhere_in_repository(
    tmp_path: Path,
) -> None:
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "shared").mkdir()
    (tmp_path / "services" / "api" / "main.py").write_text(
        "def serve(): pass\n", encoding="utf-8"
    )
    (tmp_path / "shared" / "contract.py").write_text(
        "SHARED_CONTRACT = 'v1'\n", encoding="utf-8"
    )
    config = tmp_path / ".ariadne" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        "repository:\n  file_policy: all-nonignored\n"
        "model:\n  model: fake\n  endpoint: http://unused/v1\n",
        encoding="utf-8",
    )
    backend = CrossRepositoryBackend()

    result = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="services/api",
            config_path=config,
            git_enabled=False,
            module_only=True,
            backend=backend,
        )
    )

    assert result.summary.generated == 1
    assert result.modules[0].retrieval.executed == 1
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["modules"][0]["retrieval"]["per_tool"] == {
        "search_code": 1
    }
    assert "SHARED_CONTRACT" not in json.dumps(manifest)


class LoopingBackend:
    def __init__(self) -> None:
        self.final_without_tools = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if not request.tools:
            self.final_without_tools = True
            return ModelResponse("# API\n\nBest effort.", "fake")
        return ModelResponse(
            None,
            "fake",
            (ToolCall("repeat", "list_directory", {"path": "."}),),
        )


def test_identical_call_loop_ends_with_no_tools_final_request(
    tmp_path: Path,
) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = tmp_path / ".ariadne" / "config.yaml"
    config.parent.mkdir()
    config.write_text(
        "repository:\n  file_policy: all-nonignored\n"
        "model:\n  model: fake\n  endpoint: http://unused/v1\n"
        "retrieval:\n  max_identical_calls: 1\n",
        encoding="utf-8",
    )
    backend = LoopingBackend()

    result = asyncio.run(
        weave_repository(
            cwd=tmp_path,
            path="api",
            config_path=config,
            git_enabled=False,
            module_only=True,
            backend=backend,
        )
    )

    assert backend.final_without_tools
    assert result.summary.generated == 1
    assert result.modules[0].retrieval.termination_reason == (
        "identical-call-limit"
    )
