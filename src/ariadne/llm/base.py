from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, object]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ConversationMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()

    def __init__(
        self,
        system_prompt: str | tuple[ConversationMessage, ...],
        user_prompt: str | None = None,
        *,
        tools: tuple[ToolDefinition, ...] = (),
    ) -> None:
        if isinstance(system_prompt, tuple):
            messages = system_prompt
        else:
            if user_prompt is None:
                raise TypeError("user_prompt is required with a system prompt")
            messages = (
                ConversationMessage("system", system_prompt),
                ConversationMessage("user", user_prompt),
            )
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tools", tools)

    @property
    def system_prompt(self) -> str:
        return next(
            (item.content or "" for item in self.messages if item.role == "system"),
            "",
        )

    @property
    def user_prompt(self) -> str:
        return next(
            (item.content or "" for item in self.messages if item.role == "user"),
            "",
        )


@dataclass(frozen=True)
class ModelResponse:
    text: str | None
    model: str
    tool_calls: tuple[ToolCall, ...] = ()


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
