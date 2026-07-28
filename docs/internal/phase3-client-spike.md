# Phase 3 Async Client Compatibility Spike

## Decision

Use an Ariadne-owned OpenAI-compatible adapter over `httpx.AsyncClient`.
Do not add the OpenAI Python SDK during Phase 3.

## Evaluation

Both options can issue asynchronous chat-completions requests, use configurable
base URLs, attach arbitrary authentication headers, pool connections, and
cancel in-flight requests. Ariadne currently needs only one narrow wire shape:
two text messages in and normalized text content out.

The generic client is the better fit for this milestone:

- `httpx` supplies pooling, timeouts, cancellation, mock transports, and
  well-defined transport exceptions without introducing model-provider types.
- Ariadne retains complete control of retry selection and delay. The adapter
  performs no hidden retries.
- The existing vLLM-compatible `/chat/completions` payload and both string and
  structured text-part responses remain directly covered by transport tests.
- HTTP status, `Retry-After`, invalid JSON, unsupported response shapes, and
  sanitized endpoint diagnostics are normalized into Ariadne-owned errors.
- Local and internal endpoints require no hosted-provider configuration.

The OpenAI SDK would add a larger provider-oriented surface without removing
the need to validate nonconforming OpenAI-compatible servers. No required
normalization or operational benefit was identified for Ariadne's current
single-operation interface.

## Revisit Conditions

Reconsider an SDK only when Ariadne needs a materially broader protocol surface
and the SDK demonstrates better compatibility or normalization in tests while
allowing Ariadne-controlled retries, arbitrary local endpoints, and a
provider-neutral public interface.
