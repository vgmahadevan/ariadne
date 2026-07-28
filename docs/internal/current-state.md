# Ariadne Current State

Phase 3 (Robustness) is complete. Ariadne preserves the deterministic discovery
and bounded-context generation pipeline while adding recoverable, asynchronous
repository-scale weaves.

## Current Architecture

1. `ariadne.config` owns immutable repository, module, model, context, and
   generation settings, including sequential-by-default concurrency.
2. Discovery remains isolated behind `inspect_repository()` and produces the
   immutable logical module tree consumed by generation planning.
3. `ariadne.llm` exposes an async provider-neutral backend and structured model
   errors. The OpenAI-compatible implementation uses a pooled `httpx` client.
4. `ariadne.generation` assembles context and executes individual module
   attempts, while its coordinator schedules dependency-ready modules and
   returns plan-ordered outcomes.
5. `ariadne.state` atomically persists repository-local run manifests and the
   latest-run index.
6. `ariadne.cleanup` safely selects Ariadne-owned final documents and drafts.
7. `ariadne.cli` exposes `inspect`, `weave`, and `clean`, renders deterministic
   progress and summaries, and maps complete, partial, fatal, and interrupted
   runs to distinct exit statuses.

The model receives only constructed prompts. It never controls traversal or
accesses the repository filesystem.

## Phase 3 Behavior

- Every weave creates an atomic manifest under `.ariadne/runs/` and updates
  `.ariadne/state.json`.
- A module failure is recorded and isolated. Its children are released with an
  explicit missing-new-parent-context note.
- Timeout, connection, rate-limit, server, and context-length failures receive
  at most one automatic retry. Context overflow retries at half the initial
  budget without generated-document context.
- Markdown-like invalid output is retained under `.ariadne/drafts/`; it never
  replaces a valid final document.
- `weave --resume` reconciles the latest run with the current plan, preserving
  valid compatible successes and retrying incomplete, failed, partial, changed,
  or missing entries.
- `generation.max_concurrency` and `--max-concurrency` bound active model
  requests. Parent completion controls eligibility, while final outcomes and
  terminal progress remain in deterministic plan order.
- Cancellation stops new scheduling, cancels active requests, restores their
  manifest entries to pending, and preserves completed atomic writes.
- `clean` supports repository/subtree selection, dry runs, protected-document
  handling, and optional drafts. Run history is retained.

## Public Contracts

- `LLMBackend.generate()` is asynchronous.
- `weave_repository()` is asynchronous and returns a `WeaveResult` containing
  the run ID, manifest path, ordered module outcomes, and summary.
- Progress callbacks receive structured `ProgressEvent` records.
- Model failures carry an Ariadne error kind, optional HTTP status,
  retryability, and optional retry delay.

## Deferred Features

Later phases own retrieval tools and model tool loops; bottom-up refinement and
cross-links; diagrams and website output; source fingerprints, stale detection,
and true incremental regeneration; AST/dependency indexing; and advanced
verification.

Resume reconciliation is intentionally not incremental generation: it does not
infer source staleness and only reuses valid outputs from the latest compatible
run.

## Testing Status

The model-independent suite covers discovery, configuration, generation,
provenance, atomic persistence, async OpenAI-compatible transport behavior,
structured retry selection, context-overflow degradation, partial drafts,
failure-isolated dependency scheduling, concurrency bounds, deterministic
results, resume reconciliation, cancellation recovery, CLI exit behavior, and
safe clean selection.

Tests require no network service. A directory-symlink test may skip on Windows
hosts where creating symlinks is unavailable.
