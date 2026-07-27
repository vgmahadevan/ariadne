from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from .models import ModelConfig


@dataclass(frozen=True)
class ModelRequest:
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str


class ModelErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    CONTEXT_LENGTH = "context-length"
    RATE_LIMIT = "rate-limit"
    SERVER = "server"
    INVALID_RESPONSE = "invalid-response"
    CONNECTION = "connection"


class ModelError(RuntimeError):
    def __init__(self, kind: ModelErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class LLMBackend(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse: ...


OpenUrl = Callable[..., object]


class OpenAICompatibleBackend:
    def __init__(
        self,
        config: ModelConfig,
        *,
        open_url: OpenUrl = urllib.request.urlopen,
    ) -> None:
        self.config = config
        self._open_url = open_url

    def generate(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
        }
        headers = {
            "Content-Type": "application/json",
            **dict(self.config.headers),
        }
        http_request = urllib.request.Request(
            f"{self.config.endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._open_url(
                http_request, timeout=self.config.timeout_seconds
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelError(
                _http_error_kind(exc.code, detail),
                f"model endpoint {_display_endpoint(self.config.endpoint)} "
                f"returned HTTP {exc.code}.",
            ) from exc
        except TimeoutError as exc:
            raise ModelError(
                ModelErrorKind.TIMEOUT,
                f"model endpoint {_display_endpoint(self.config.endpoint)} timed out "
                f"after {self.config.timeout_seconds:g} seconds.",
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            kind = (
                ModelErrorKind.TIMEOUT
                if isinstance(reason, TimeoutError)
                else ModelErrorKind.CONNECTION
            )
            description = (
                f"timed out after {self.config.timeout_seconds:g} seconds"
                if kind is ModelErrorKind.TIMEOUT
                else "could not be reached"
            )
            raise ModelError(
                kind,
                f"model endpoint {_display_endpoint(self.config.endpoint)} "
                f"{description}.",
            ) from exc
        try:
            data = json.loads(raw)
            text = _extract_text(data)
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
        if not isinstance(text, str) or not text.strip():
            raise ModelError(
                ModelErrorKind.INVALID_RESPONSE,
                f"model endpoint {_display_endpoint(self.config.endpoint)} returned "
                "an empty choices[0].message.content value.",
            )
        return ModelResponse(text=text, model=str(response_model))


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
    return ModelErrorKind.SERVER


def _extract_text(data: object) -> str:
    if not isinstance(data, dict):
        raise TypeError("response must be an object")
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                raise TypeError("content part must be an object")
            if item.get("type") in {"text", "output_text"} and isinstance(
                item.get("text"), str
            ):
                parts.append(item["text"])
        if not parts:
            raise ValueError("content has no text parts")
        return "".join(parts)
    raise TypeError("content must be text or text parts")


def _display_endpoint(endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )
