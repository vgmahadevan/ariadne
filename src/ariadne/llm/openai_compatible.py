from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..settings import ModelConfig
from .base import (
    ConversationMessage,
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ModelResponse,
    ToolCall,
)


class OpenAICompatibleBackend:
    def __init__(
        self,
        config: ModelConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "model": self.config.model,
            "messages": [_message_payload(item) for item in request.messages],
            "max_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                headers=dict(self.config.headers),
                timeout=self.config.timeout_seconds,
            )
            self._client = client
        try:
            response = await client.post(
                f"{self.config.endpoint}/chat/completions",
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise ModelError(
                ModelErrorKind.TIMEOUT,
                f"model endpoint {_display_endpoint(self.config.endpoint)} "
                f"timed out after {self.config.timeout_seconds:g} seconds.",
            ) from exc
        except httpx.RequestError as exc:
            raise ModelError(
                ModelErrorKind.CONNECTION,
                f"model endpoint {_display_endpoint(self.config.endpoint)} "
                "could not be reached.",
            ) from exc
        if response.is_error:
            detail = response.text
            kind = _http_error_kind(response.status_code, detail)
            raise ModelError(
                kind,
                f"model endpoint {_display_endpoint(self.config.endpoint)} "
                f"returned HTTP {response.status_code}.",
                status_code=response.status_code,
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )
        try:
            data = response.json()
            text, tool_calls = _extract_message(data)
            response_model = data.get("model", self.config.model)
        except json.JSONDecodeError as exc:
            raise ModelError(
                ModelErrorKind.INVALID_RESPONSE,
                f"model endpoint {_display_endpoint(self.config.endpoint)} returned "
                "non-JSON data; expected an OpenAI-compatible chat-completions response.",
            ) from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError(
                ModelErrorKind.INVALID_RESPONSE,
                f"model endpoint {_display_endpoint(self.config.endpoint)} returned "
                "JSON in an unsupported format; expected text at "
                "choices[0].message.content.",
            ) from exc
        if (not isinstance(text, str) or not text.strip()) and not tool_calls:
            raise ModelError(
                ModelErrorKind.INVALID_RESPONSE,
                f"model endpoint {_display_endpoint(self.config.endpoint)} returned "
                "an empty choices[0].message.content value.",
            )
        return ModelResponse(
            text=text if isinstance(text, str) and text.strip() else None,
            model=str(response_model),
            tool_calls=tool_calls,
        )


def _http_error_kind(status: int, detail: str) -> ModelErrorKind:
    if status in {401, 403}:
        return ModelErrorKind.AUTHENTICATION
    if status == 429:
        return ModelErrorKind.RATE_LIMIT
    if status >= 500:
        return ModelErrorKind.SERVER
    lowered = detail.lower()
    if status == 400 and ("context" in lowered or "token" in lowered):
        return ModelErrorKind.CONTEXT_LENGTH
    return ModelErrorKind.INVALID_RESPONSE


def _message_payload(message: ConversationMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role}
    if message.content is not None:
        payload["content"] = message.content
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, sort_keys=True),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        payload["name"] = message.name
    return payload


def _extract_message(data: object) -> tuple[str | None, tuple[ToolCall, ...]]:
    if not isinstance(data, dict):
        raise TypeError("response must be an object")
    message = data["choices"][0]["message"]
    if not isinstance(message, dict):
        raise TypeError("message must be an object")
    content = message.get("content")
    text: str | None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                raise TypeError("content part must be an object")
            if item.get("type") in {"text", "output_text"} and isinstance(
                item.get("text"), str
            ):
                parts.append(item["text"])
        if not parts:
            text = None
        else:
            text = "".join(parts)
    elif content is None:
        text = None
    else:
        raise TypeError("content must be text, text parts, or null")
    calls: list[ToolCall] = []
    raw_calls = message.get("tool_calls", [])
    if not isinstance(raw_calls, list):
        raise TypeError("tool_calls must be a list")
    for item in raw_calls:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise TypeError("tool call must be a function object")
        function = item.get("function")
        if not isinstance(function, dict):
            raise TypeError("tool function must be an object")
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        calls.append(
            ToolCall(str(item["id"]), str(function["name"]), arguments)
        )
    return text, tuple(calls)


def _display_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            delay = (
                parsedate_to_datetime(value) - datetime.now(timezone.utc)
            ).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, delay)
