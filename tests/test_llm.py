import json
import urllib.error

import pytest

from ariadne.llm import (
    ModelError,
    ModelErrorKind,
    ModelRequest,
    OpenAICompatibleBackend,
)
from ariadne.models import ModelConfig


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_openai_compatible_backend_uses_chat_completions_shape() -> None:
    captured = {}

    def open_url(request, *, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "model": "served-vllm-model",
                "choices": [{"message": {"content": "# Generated"}}],
            }
        )

    config = ModelConfig(
        model="requested-model",
        endpoint="http://vllm:8000/v1",
        timeout_seconds=12,
        headers=(("Authorization", "Bearer secret"), ("X-Tenant", "docs")),
    )
    response = OpenAICompatibleBackend(config, open_url=open_url).generate(
        ModelRequest("system", "user")
    )

    assert captured["url"] == "http://vllm:8000/v1/chat/completions"
    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert captured["payload"]["max_tokens"] == config.max_output_tokens
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["timeout"] == 12
    assert response.text == "# Generated"
    assert response.model == "served-vllm-model"


def test_backend_classifies_context_errors_without_leaking_response() -> None:
    def open_url(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "bad request",
            {},
            FakeResponse({"error": "maximum context tokens exceeded"}),
        )

    with pytest.raises(ModelError) as captured:
        OpenAICompatibleBackend(ModelConfig(), open_url=open_url).generate(
            ModelRequest("system", "user")
        )

    assert captured.value.kind is ModelErrorKind.CONTEXT_LENGTH
    assert "maximum context" not in str(captured.value)


def test_backend_accepts_structured_text_content() -> None:
    def open_url(request, *, timeout):
        return FakeResponse(
            {
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
            }
        )

    response = OpenAICompatibleBackend(
        ModelConfig(), open_url=open_url
    ).generate(ModelRequest("system", "user"))

    assert response.text == "# Module\n\nSummary"


def test_invalid_response_explains_expected_shape_and_hides_query() -> None:
    def open_url(request, *, timeout):
        return FakeResponse({"output": "different API"})

    config = ModelConfig(endpoint="http://service.test/v1?api_key=secret")
    with pytest.raises(ModelError) as captured:
        OpenAICompatibleBackend(config, open_url=open_url).generate(
            ModelRequest("system", "user")
        )

    message = str(captured.value)
    assert "choices[0].message.content" in message
    assert "api_key" not in message
    assert "secret" not in message


def test_connection_error_names_sanitized_endpoint() -> None:
    def open_url(request, *, timeout):
        raise urllib.error.URLError("connection refused")

    config = ModelConfig(endpoint="http://localhost:9999/v1?token=secret")
    with pytest.raises(ModelError) as captured:
        OpenAICompatibleBackend(config, open_url=open_url).generate(
            ModelRequest("system", "user")
        )

    assert captured.value.kind is ModelErrorKind.CONNECTION
    assert "http://localhost:9999/v1" in str(captured.value)
    assert "token" not in str(captured.value)
    assert "secret" not in str(captured.value)
