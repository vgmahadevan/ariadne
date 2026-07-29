from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ..settings import ModelConfig
from .base import (
    ModelError,
    ModelErrorKind,
    ModelRequest,
    ModelResponse,
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
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
        }
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
    return ModelErrorKind.INVALID_RESPONSE


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
