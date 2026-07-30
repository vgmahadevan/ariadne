# Ariadne Current State

Phase 3 (Robustness) is complete. Ariadne can inspect a repository, plan one
document per logical module, generate those documents through an asynchronous
OpenAI-compatible model, isolate module failures, persist recoverable run
state, resume incomplete work, and safely clean generated artifacts.

## Current Architecture

### Configuration and entrypoint

- `ariadne.settings` defines immutable repository, module, model, context, and
  generation settings.
- `ariadne.config` discovers, initializes, parses, and validates
  `.ariadne/config.yaml`. It also creates the non-overwriting
  `.ariadne/README.md` and ensures `.ariadne/` is ignored by Git.
- `ariadne.cli` exposes `inspect`, `weave`, and `clean`. It owns terminal
  rendering, progress timing, summaries, and exit-code mapping; library code
  communicates through results and callbacks.

### Repository discovery

- `ariadne.discovery.repository` resolves roots and selections, reads Git state,
  and obtains the source commit.
- `ariadne.discovery.scanner` applies ignore and file-policy rules and records
  deterministic physical file metadata.
- `ariadne.discovery.modules` constructs the logical module tree and collapses
  structural directory chains. The full-repository root module is named
  `repository`; subtree roots retain their physical module name.
- `ariadne.discovery.inspect_repository()` is the stable boundary returning an
  immutable `InspectionResult`.

### Model boundary

- `ariadne.llm.base` defines provider-neutral requests, responses, the async
  `LLMBackend` protocol, and structured model errors.
- `ariadne.llm.openai_compatible` uses a pooled `httpx.AsyncClient` for
  `/chat/completions`, normalizes string and structured text responses, and
  classifies HTTP, timeout, connection, context-length, and invalid-response
  failures.
- Provider code receives constructed prompts only. It has no filesystem,
  traversal, retry-policy, state, or persistence responsibilities.

### Weave pipeline

The generation flow is:

```text
Inspect -> Plan -> Schedule eligible module -> Assemble context
        -> Execute bounded attempts -> Validate -> Persist atomically
        -> Record state -> Release children
```

- `ariadne.weave.planning` owns deterministic traversal, output naming,
  collision handling, parent relationships, and stable module IDs.
- `ariadne.weave.context` owns bounded evidence selection and the prompt
  contract. The prompt has an explicit version used by resume fingerprints.
- `ariadne.weave.documents` owns provenance, validation, metadata parsing,
  partial drafts, overwrite protection, and atomic writes.
- `ariadne.weave.executor` owns one module's attempts and error-to-outcome
  conversion.
- `ariadne.weave.runner` owns run setup, dependency-aware scheduling,
  concurrency, deterministic progress ordering, cancellation, and summaries.
- `ariadne.weave.state` atomically persists `.ariadne/state.json` and versioned
  run manifests beneath `.ariadne/runs/`.
- `ariadne.weave.cleanup` selects only provenance-marked Ariadne documents and
  drafts within the requested repository subtree.

## Implemented Features

### Discovery and planning

- Repository-root and subtree resolution with path-containment checks.
- Git-aware tracked, untracked, ignored, and no-Git file policies.
- Default ignores, configurable include/exclude patterns, and symlink safety.
- Deterministic logical-module discovery and structural-directory collapsing.
- Repository and subtree inspection with hierarchy, language, path, and ignore
  information.
- One deterministic `*-genai-doc.md` destination per logical module with
  collision disambiguation.

### Model and context handling

- Async provider-neutral generation interface.
- Pooled OpenAI-compatible HTTP adapter with arbitrary headers and configurable
  model, endpoint, context window, output limit, temperature, and timeout.
- Sanitized endpoint diagnostics that exclude headers, response bodies, and
  endpoint query parameters.
- Bounded context assembly with deterministic file priorities, per-file limits,
  total estimated token limits, truncation notes, binary omission, local tree
  context, parent/ancestor documents, and evidence labels.
- Stable prompt contract with leaf-module guidance, source-grounding rules, and
  explicit uncertainty requirements.
- Async client compatibility decision documented in
  `docs/internal/phase3-client-spike.md`.

### Robust generation

- Atomic provenance-bearing final documents; provenance and atomic writes are
  enforced invariants rather than optional behavior.
- Protection for non-Ariadne, human-reviewed, and human-modified documents.
- Per-module failure isolation for model, validation, persistence, repository,
  and unexpected internal errors.
- Two total attempts for retryable timeout, connection, rate-limit, server, and
  context-length failures, with bounded `Retry-After` handling.
- Context-overflow retry at half the initial context budget with generated
  document context disabled.
- Markdown-like invalid output retained beneath `.ariadne/drafts/` without
  replacing an existing valid document.
- Descendants released after a failed parent and given an explicit
  missing-new-parent-document omission.
- Dependency-aware asynchronous scheduling with configurable concurrency,
  defaulting to eight active model requests.
- Deterministic plan-ordered results and terminal progress despite
  out-of-order task completion.
- In-place progress with elapsed time, estimated remaining time, and final
  duration.
- Prompt cancellation: no new modules are scheduled, active requests are
  cancelled, completed writes remain, and interrupted entries return to
  `pending`.

### State, resume, reporting, and cleanup

- Atomic per-run manifests containing the ordered plan, module identity,
  outputs, parent outputs, attempts, timestamps, model, structured failure
  information, prompt version, and summary.
- `weave --resume` reconciles the latest run for the same repository selection,
  reuses valid compatible successes, and retries failed, partial, pending,
  changed, new, or missing-output modules.
- Resume fingerprints include prompt- and output-affecting settings but exclude
  operational settings such as concurrency, timeout, and overwrite policy.
- Final generated, updated, failed, and partial counts with failed module paths
  and error classes.
- Distinct CLI statuses for complete success, completed runs with module
  failures, fatal setup errors, and user cancellation.
- Safe whole-repository and subtree cleanup, dry runs, protected-document
  handling, and optional draft removal while preserving run history.

## Phase 3 Milestone Status

### Fully implemented

- Persistent run state and atomic manifests.
- Model timeouts and structured error classification.
- Module failure isolation and descendant continuation.
- Partial draft handling and preservation of prior valid documents.
- Bounded automatic retries and context-overflow degradation.
- Resume of incomplete or failed work.
- Safe generated-document and draft cleanup.
- Async provider-neutral model invocation.
- Bounded dependency-aware concurrency and a configurable default.
- Deterministic state, progress, summary, and result coordination.
- Graceful cancellation of in-flight model requests.
- Async generic-client versus SDK evaluation.

### Partially implemented

- **Retry CLI:** failed modules are retried through `weave --resume`; there is
  no separate `ariadne retry`, historical run selector, or stale/missing-only
  selector. This satisfies Phase 3 recovery for the latest run but not the
  specification's broader eventual CLI.
- **Run metadata:** manifests contain the information required for Phase 3
  resume and debugging, but not source fingerprints, stale status, corpus-wide
  last-success records, warning histories, or child-ID lists.
- **Context-overflow recovery:** one deterministic reduced-context retry is
  implemented. Structural summaries, analysis splitting, targeted retrieval,
  and tool-result reduction require later phases.
- **Partial output:** complete model responses that fail validation can become
  drafts. Interrupted HTTP streams are not captured as partial responses.
- **Summary and observability:** generated, updated, failed, and partial states
  are reported. Unchanged, skipped, stale, needs-review, persistent log files,
  per-module latency, and machine-readable CLI output are not implemented.

These differences do not block Phase 4. A separate retry command would largely
duplicate current resume selection; stale and historical selection depend on
incremental state that belongs to Phase 7.

## Deferred Features

- Phase 4 repository retrieval tools, tool-call budgets, path-bounded tool
  execution, loop detection, and tool-error recovery.
- Bottom-up traversal, second-pass refinement, child-document context,
  cross-links, and consistency refinement.
- Mermaid diagrams, a documentation website, rendered navigation, and search.
- Source fingerprints, stale detection, incremental regeneration, ancestor
  invalidation, and corpus manifests.
- AST or dependency graphs, symbol-level verification, coverage metrics, and
  advanced consistency checks.
- Secret redaction, structured logging, machine-readable CLI output, arbitrary
  historical-run selection, and corpus status reporting.

No file-level documentation is planned.

## Important Extension Points

- `inspect_repository()` remains the only discovery entrypoint. Retrieval
  should consume its repository context and physical/logical models rather than
  adding model concerns to scanning.
- `LLMBackend` is the provider boundary. Phase 4 must keep SDK-specific request,
  tool-call, and response types behind adapters.
- `ModelRequest` currently represents one system prompt and one user prompt.
  Phase 4 should introduce provider-neutral conversation/tool records here
  rather than embedding a provider's function-call schema into weave code.
- `weave.context` owns initial evidence and prompt construction. Retrieval
  results should be added as explicitly labeled, bounded evidence without
  granting providers filesystem access.
- `weave.executor.execute_module()` is the correct location for a harness-owned
  model/tool interaction loop. The dependency scheduler should continue to see
  one terminal module outcome, regardless of how many bounded model/tool turns
  occur internally.
- `weave.runner` should remain responsible only for module dependency
  scheduling, concurrency, cancellation, progress, and run-level results.
- `weave.state` owns manifest schema and atomic transitions. Any Phase 4
  attempt/tool metadata should be summarized here without storing full source
  contents or secrets.
- `weave.documents` remains the final-output trust boundary. Tool protocol
  leakage checks belong in validation, not in provider adapters.
- `weave.cleanup` must continue using exact suffix, provenance, and repository
  containment rules as new internal artifact types are introduced.

## Technical Debt

- Run manifests are represented internally as mutable dictionaries with
  shallow load-time validation. Typed manifest records or centralized schema
  validation would reduce migration risk before the schema grows substantially.
- Context assembly and atomic state writes perform synchronous filesystem work
  on the event-loop thread. This keeps coordination deterministic but may
  become noticeable on very large repositories or slow filesystems.
- Prompt construction is a single long template function. Phase 4 should
  compose retrieval/tool instructions without duplicating the base
  documentation contract.
- Retry count and degradation policy are fixed rather than configurable.
- Resume targets only the latest run and requires the same repository
  selection. Earlier runs cannot be selected directly.
- Context budgeting uses a deterministic character estimate rather than a
  provider tokenizer.
- Markdown validation is structural and does not yet validate links,
  repository-path containment, or obvious future tool-protocol leakage.
- Error diagnostics are sanitized, but there is no persistent structured
  logging or per-module latency record.
- The OpenAI-compatible adapter is tested at the wire-contract level with mock
  transports; no live vLLM service is required by the suite.
- Tests collect no numeric coverage metric. One symlink test may skip on
  Windows hosts without directory-symlink privileges.

## Testing Status

The test suite is model-independent and requires no network service. It covers:

- configuration discovery, initialization, required persistence invariants,
  validation, and `.ariadne` documentation;
- repository resolution, path safety, Git policies, ignore precedence,
  scanning, language detection, symlink handling, module collapse, and fixture
  integrations;
- planning order, naming, collision handling, context selection and budgeting,
  evidence labels, prompt construction, provenance, validation, overwrite
  protection, and atomic writes;
- OpenAI-compatible request/response normalization, header handling, sanitized
  errors, rate-limit metadata, timeout, context-length, invalid-response, and
  connection classification;
- failure isolation, bounded retries, context degradation, partial drafts,
  parent-failure continuation, concurrency limits, deterministic progress,
  resume reconciliation, cancellation recovery, and fingerprint selection;
- safe cleanup and CLI behavior, progress rendering, summaries, and exit codes.

The Phase 3 close-out suite reports 53 passed tests and one skipped
Windows-dependent symlink test. No numeric line or branch coverage threshold is
configured.

## Recommendations for Phase 4

1. Define provider-neutral tool request/result and conversation records before
   changing any provider adapter.
2. Implement the bounded tool loop inside `weave.executor`, leaving
   `weave.runner` and dependency scheduling unchanged.
3. Add retrieval results compositionally through `weave.context` with explicit
   evidence labels, size limits, path containment, call budgets, and repeated
   call detection.
4. Extend manifest schema deliberately for bounded attempt/tool summaries and
   bump its schema version; do not persist source contents or secrets.
5. Add validation for obvious tool-protocol leakage before enabling tool use.
6. Keep retrieval independently disableable and preserve the current no-tools
   generation path as the baseline.
