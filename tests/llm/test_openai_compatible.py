from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ariadne.llm import (
    ConversationMessage,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    OpenAICompatibleBackend,
    ToolCall,
    ToolDefinition,
)
from ariadne.settings import ModelConfig


def _run(backend: OpenAICompatibleBackend):
    async def invoke():
        try:
            return await backend.generate(ModelRequest("system", "user"))
        finally:
            await backend.aclose()

    return asyncio.run(invoke())


def test_openai_compatible_backend_uses_chat_completions_shape() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "served-vllm-model",
                "choices": [{"message": {"content": "# Generated"}}],
            },
        )

    config = ModelConfig(
        model="requested-model",
        endpoint="http://vllm:8000/v1",
        timeout_seconds=12,
        headers=(("Authorization", "Bearer secret"), ("X-Tenant", "docs")),
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=dict(config.headers),
        timeout=config.timeout_seconds,
    )
    response = _run(OpenAICompatibleBackend(config, client=client))

    assert captured["url"] == "http://vllm:8000/v1/chat/completions"
    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert captured["payload"]["max_tokens"] == config.max_output_tokens
    assert captured["headers"]["authorization"] == "Bearer secret"
    assert response.text == "# Generated"
    assert response.model == "served-vllm-model"
    asyncio.run(client.aclose())


def test_backend_round_trips_native_tool_calls() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "fake",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"shared/api.py"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAICompatibleBackend(ModelConfig(), client=client)
    request = ModelRequest(
        (
            ConversationMessage("system", "system"),
            ConversationMessage(
                "assistant",
                tool_calls=(ToolCall("old", "read_file", {"path": "old.py"}),),
            ),
            ConversationMessage(
                "tool", "result", tool_call_id="old", name="read_file"
            ),
        ),
        tools=(
            ToolDefinition(
                "read_file",
                "Read a file.",
                {"type": "object", "properties": {}},
            ),
        ),
    )

    response = asyncio.run(backend.generate(request))

    assert captured["payload"]["tools"][0]["function"]["name"] == "read_file"
    assert captured["payload"]["messages"][1]["tool_calls"][0]["id"] == "old"
    assert captured["payload"]["messages"][2]["tool_call_id"] == "old"
    assert response.text is None
    assert response.tool_calls == (
        ToolCall("call-1", "read_file", {"path": "shared/api.py"}),
    )
    asyncio.run(client.aclose())


def test_backend_classifies_context_errors_without_leaking_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "maximum context tokens exceeded"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError) as captured:
        _run(OpenAICompatibleBackend(ModelConfig(), client=client))

    assert captured.value.kind is ModelErrorKind.CONTEXT_LENGTH
    assert captured.value.status_code == 400
    assert captured.value.retryable
    assert "maximum context" not in str(captured.value)
    asyncio.run(client.aclose())


def test_backend_accepts_structured_text_content() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "# Module\n"},
                                {"type": "output_text", "text": "\nSummary"},
                            ]
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = _run(OpenAICompatibleBackend(ModelConfig(), client=client))
    assert response.text == "# Module\n\nSummary"
    asyncio.run(client.aclose())


def test_rate_limit_carries_bounded_retry_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError) as captured:
        _run(OpenAICompatibleBackend(ModelConfig(), client=client))
    assert captured.value.kind is ModelErrorKind.RATE_LIMIT
    assert captured.value.retry_after == 12
    asyncio.run(client.aclose())


def test_invalid_response_explains_expected_shape_and_hides_query() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": "different API"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ModelConfig(endpoint="http://service.test/v1?api_key=secret")
    with pytest.raises(ModelError) as captured:
        _run(OpenAICompatibleBackend(config, client=client))

    message = str(captured.value)
    assert "choices[0].message.content" in message
    assert "api_key" not in message
    assert "secret" not in message
    asyncio.run(client.aclose())


def test_connection_error_names_sanitized_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ModelConfig(endpoint="http://localhost:9999/v1?token=secret")
    with pytest.raises(ModelError) as captured:
        _run(OpenAICompatibleBackend(config, client=client))

    assert captured.value.kind is ModelErrorKind.CONNECTION
    assert "http://localhost:9999/v1" in str(captured.value)
    assert "token" not in str(captured.value)
    assert "secret" not in str(captured.value)
    asyncio.run(client.aclose())


def test_timeout_is_classified_as_retryable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError) as captured:
        _run(OpenAICompatibleBackend(ModelConfig(), client=client))

    assert captured.value.kind is ModelErrorKind.TIMEOUT
    assert captured.value.retryable
    asyncio.run(client.aclose())
