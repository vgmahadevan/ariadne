from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Awaitable, Callable

from ..discovery.models import InspectionResult
from ..llm import (
    ConversationMessage,
    LLMBackend,
    ModelError,
    ModelErrorKind,
    ModelRequest,
)
from ..settings import AriadneConfig
from .context import assemble_context, build_prompt
from .documents import (
    PersistenceError,
    ValidationError,
    compose_document,
    is_markdown_like,
    persist_document,
    persist_partial_draft,
    validate_document,
)
from .models import GenerationResult, ModuleStatus, PlannedModule
from .retrieval import RetrievalHarness, RetrievalSummary


async def execute_module(
    inspection: InspectionResult,
    plan: PlannedModule,
    config: AriadneConfig,
    backend: LLMBackend,
    commit: str | None,
    *,
    force: bool,
    missing_parent: bool,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    run_id: str,
    on_attempt: Callable[[int], None],
    retrieval_inspection: InspectionResult | None = None,
) -> GenerationResult:
    existed = plan.output.is_file()
    response_text: str | None = None
    response_model: str | None = None
    reduce_context = False
    retrieval_summary = RetrievalSummary()
    for attempt in (1, 2):
        harness: RetrievalHarness | None = None
        on_attempt(attempt)
        attempt_config = config
        if attempt == 2 and reduce_context:
            attempt_config = replace(
                config,
                context=replace(
                    config.context,
                    max_initial_tokens=max(
                        1, config.context.max_initial_tokens // 2
                    ),
                    include_generated_docs=False,
                ),
            )
        try:
            context = assemble_context(
                inspection,
                plan,
                attempt_config,
                source_commit_value=commit,
                missing_parent=missing_parent,
            )
            prompt = build_prompt(
                context,
                retrieval_enabled=(
                    config.retrieval.enabled
                    and retrieval_inspection is not None
                ),
            )
            harness = (
                RetrievalHarness(retrieval_inspection, config.retrieval)
                if config.retrieval.enabled and retrieval_inspection is not None
                else None
            )
            response = await _generate_with_retrieval(
                backend, prompt, harness
            )
            if harness is not None:
                retrieval_summary = harness.summary()
            response_text = response.text
            response_model = response.model
            if response.text is None:
                raise ModelError(
                    ModelErrorKind.INVALID_RESPONSE,
                    "model did not return a final Markdown document.",
                    retryable=False,
                )
            document = compose_document(
                response.text,
                config=config,
                module=plan.module,
                generated_at=clock(),
                source_commit_value=commit,
                model=response.model,
            )
            validate_document(document)
            persist_document(plan.output, document, config=config, force=force)
            if not plan.output.is_file():
                raise PersistenceError(
                    "documentation output does not exist after persistence: "
                    f"{plan.output}"
                )
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                (
                    ModuleStatus.UPDATED.value
                    if existed
                    else ModuleStatus.GENERATED.value
                ),
                response.model,
                attempt,
                retrieval=retrieval_summary,
            )
        except asyncio.CancelledError:
            raise
        except ModelError as exc:
            if harness is not None:
                retrieval_summary = harness.summary()
            if attempt == 1 and exc.retryable:
                reduce_context = exc.kind is ModelErrorKind.CONTEXT_LENGTH
                delay = exc.retry_after if exc.retry_after is not None else 1.0
                await sleep(min(30.0, delay))
                continue
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                ModuleStatus.FAILED.value,
                response_model,
                attempt,
                exc.kind.value,
                str(exc),
                None,
                exc.status_code,
                exc.retryable,
                retrieval_summary,
            )
        except (ValidationError, PersistenceError, OSError) as exc:
            draft_path = None
            if response_text is not None and is_markdown_like(response_text):
                try:
                    draft_path = persist_partial_draft(
                        inspection.context.root,
                        run_id,
                        plan,
                        response_text,
                        response_model or config.model.model,
                        clock(),
                    )
                except PersistenceError as draft_exc:
                    exc = draft_exc
            kind = (
                "validation"
                if isinstance(exc, ValidationError)
                else "persistence"
            )
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                (
                    ModuleStatus.PARTIAL.value
                    if draft_path is not None
                    else ModuleStatus.FAILED.value
                ),
                response_model,
                attempt,
                kind,
                str(exc),
                draft_path,
                retrieval=retrieval_summary,
            )
        except Exception as exc:
            return GenerationResult(
                plan.module.physical_path,
                plan.output,
                ModuleStatus.FAILED.value,
                response_model,
                attempt,
                "internal",
                f"{type(exc).__name__}: {exc}",
                retrieval=retrieval_summary,
            )
    raise AssertionError("module attempts exhausted")


async def _generate_with_retrieval(
    backend: LLMBackend,
    request: ModelRequest,
    harness: RetrievalHarness | None,
):
    if harness is None or not harness.definitions:
        return await backend.generate(request)
    messages = list(request.messages)
    while True:
        response = await backend.generate(
            ModelRequest(tuple(messages), tools=harness.definitions)
        )
        if not response.tool_calls:
            return response
        messages.append(
            ConversationMessage(
                "assistant",
                response.text,
                tool_calls=response.tool_calls,
            )
        )
        for call in response.tool_calls:
            result = await harness.execute(call)
            messages.append(
                ConversationMessage(
                    "tool",
                    result,
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
        if harness.termination_reason is not None:
            messages.append(
                ConversationMessage(
                    "user",
                    "Retrieval has ended. Using only the evidence already supplied, "
                    "return the best-effort final Markdown document now. Do not emit "
                    "tool calls or tool protocol data.",
                )
            )
            return await backend.generate(ModelRequest(tuple(messages)))
