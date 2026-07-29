from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


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
    def __init__(
        self,
        kind: ModelErrorKind,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retryable = (
            kind
            in {
                ModelErrorKind.TIMEOUT,
                ModelErrorKind.CONNECTION,
                ModelErrorKind.RATE_LIMIT,
                ModelErrorKind.SERVER,
                ModelErrorKind.CONTEXT_LENGTH,
            }
            if retryable is None
            else retryable
        )
        self.retry_after = retry_after


class LLMBackend(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
