# Ariadne Current State

Phase 2 (LLM Integration and Documentation Generation) is complete. Ariadne
retains the deterministic Phase 1 inspection pipeline and can now generate one
Markdown document per logical module through an OpenAI-compatible model
endpoint.

## Current Architecture

The package remains a small `src/ariadne` application with immutable dataclasses
as its main boundaries:

1. `ariadne.config` discovers, initializes, parses, and validates the aggregate
   `AriadneConfig`. Repository, module, model, context, and generation settings
   are separate frozen configuration records.
2. `ariadne.repository`, `ariadne.git`, `ariadne.scanner`, and
   `ariadne.modules` implement deterministic Phase 1 discovery.
   `inspect_repository()` remains the boundary that returns repository context,
   physical nodes, ignored paths, and the logical module tree.
3. `ariadne.llm` defines the provider-neutral `LLMBackend` protocol,
   request/response records, classified model errors, and the synchronous
   OpenAI-compatible `/chat/completions` reference adapter.
4. `ariadne.generation` plans the top-down module order, assembles bounded
   evidence, builds prompts, invokes the backend, composes provenance,
   validates Markdown structure, and persists documents.
5. `ariadne.cli` exposes `inspect` and `weave`. `ariadne.render` formats the
   inspection hierarchy and planned-document count. Weave progress is emitted
   through callbacks and rendered on stderr; generated paths remain on stdout.

The model backend receives only constructed prompts. It does not access the
repository filesystem or control traversal.

The generation path is:

```text
Discover -> Plan Top-Down -> Assemble Context -> Invoke Model
         -> Compose Provenance -> Validate -> Persist Atomically
```

## Implemented Features

### Discovery and planning

- All Phase 1 repository discovery and `ariadne inspect [path]` behavior.
- Deterministic logical-module ordering and top-down traversal.
- Subtree generation by default and `--module-only` generation.
- Planned documentation count in inspect output.
- One deterministic filesystem-safe `*-genai-doc.md` destination per logical
  module, including deterministic collision disambiguation.
- Repository-wide generation when `weave` is invoked without a path.

### Configuration and model abstraction

- Typed aggregate configuration for repository, modules, model, context, and
  generation behavior.
- First-weave creation of `.ariadne/config.yaml` and addition of `.ariadne/` to
  `.gitignore`.
- Configurable OpenAI-compatible endpoint, model, context window, output limit,
  temperature, timeout, and arbitrary string headers.
- Provider-neutral model request/response protocol.
- OpenAI-compatible chat-completions HTTP adapter with string and structured
  text-part response support.
- Model error classification for authentication, timeout, connection,
  rate-limit, context-length, server, and invalid-response failures.
- Sanitized endpoint diagnostics that do not expose configured headers,
  endpoint query parameters, or response bodies.
- Mocked transport coverage for the request/response shape used by common vLLM
  OpenAI-compatible servers.

### Context and prompting

- Repository identity, root, source commit when available, module location,
  ancestor names, languages, child modules, and bounded local tree context.
- Deterministic file selection prioritizing manifests/configuration, entry
  points, source, human documentation, tests, miscellaneous text, and prior
  generated documentation.
- Per-file byte limits and a stable character-to-token estimate for the total
  initial-context budget.
- Binary-content omission, large-file truncation, and explicit omission notes.
- Evidence labels:
  - source/configuration as primary evidence;
  - human-authored documentation as secondary evidence;
  - prior generated documentation as unverified, potentially stale evidence.
- Optional inclusion of the current prior document, ancestor documents, and the
  exact traversal-parent document.
- Prompt instructions for source grounding, uncertainty, concrete files and
  symbols, flexible document structure, and leaf-level sibling-file detail.
- Generated documents remain excluded from source discovery, module boundaries,
  language totals, and source-size totals.

### Generation, provenance, validation, and persistence

- Sequential model invocation with generated parent context available to
  descendants.
- Harness-owned YAML front matter and visible AI-generation disclaimer.
- Generation timestamp, Ariadne version, model, logical module, source commit
  when available, status, and human-review/modification metadata.
- Lightweight validation for nonempty output, parseable front matter, required
  provenance, the AI disclaimer, and exactly one nonempty level-one title.
- Validation before destination replacement and an existence check afterward.
- Temporary-file creation in the destination directory, flush, `fsync`, and
  atomic replacement.
- Protection for non-Ariadne, human-reviewed, and human-modified documents,
  with explicit `--force` override.
- Module-based progress events that advance after successful validation and
  persistence.
- Actionable configuration and model-connection errors.

## Milestone Status

The Phase 2 roadmap deliverables are fully implemented: model adapter, prompt
template, top-down traversal, bounded local context, module-level documentation,
provenance, deterministic names, atomic persistence, subtree operation, and the
`ariadne weave path/to/module` command.

The following specification areas are intentionally lightweight or partial:

- Markdown validation checks the required structure but does not use a full
  CommonMark parser or validate every link and repository-relative path.
- Context budgeting uses a configurable character estimate rather than a
  provider tokenizer.
- Binary detection is based on NUL bytes and decoding uses UTF-8 replacement.
- A selected subtree is discovered as its own logical root, so its prompt does
  not reconstruct the full logical ancestor chain. Existing generated ancestor
  documents may still be included as lower-confidence context.
- vLLM compatibility is tested at the OpenAI-compatible wire-contract level
  with a mocked transport; the suite does not require a live vLLM server.
- Error classification is sufficient for single-attempt Phase 2 generation but
  does not yet carry structured retryability, HTTP status, or attempt metadata.

These limitations do not prevent the Phase 2 deliverable. Link verification,
provider tokenization, richer recovery metadata, and robust failure handling
belong to later milestones.

## Deferred Features

Phase 3 intentionally owns:

- persistent run state and manifests;
- module failure isolation and run summaries;
- retries, resume, partial drafts, and safe cleanup;
- context-overflow recovery policies;
- async model invocation and bounded dependency-aware concurrency;
- cancellation and coordination of out-of-order progress/state updates.

Later phases own:

- retrieval tools, search, and model tool-call loops;
- bottom-up traversal, child-document refinement, second passes, and cross-links;
- AST or dependency indexing;
- Mermaid diagrams, a documentation website, and rendered navigation;
- incremental regeneration, source fingerprints, stale detection, and advanced
  verification.

No file-level documentation is planned.

## Important Extension Points

- `inspect_repository()` is the discovery boundary. Future generation behavior
  should consume `InspectionResult` rather than add model concerns to scanning.
- `plan_modules()` produces immutable `PlannedModule` records with exact parent
  and output relationships. Phase 3 scheduling should consume this plan rather
  than re-derive dependencies from filesystem paths.
- `assemble_context()` is the evidence boundary. Missing-parent status, retry
  context reductions, and later retrieval results should be represented here
  without giving providers filesystem access.
- `build_prompt()` owns the documentation contract and evidence instructions.
  Retrieval/tool protocol instructions should be added compositionally in
  Phase 4 rather than embedded in discovery.
- `LLMBackend` is the provider boundary. Phase 3 is expected to make generation
  asynchronous after comparing an established async HTTP client with an
  OpenAI-compatible SDK. SDK-specific types must not leak through this protocol.
- The current sequential loop in `weave_repository()` is the orchestration
  boundary. Before adding concurrency, extract a single-planned-module executor
  and wrap it with persistent state and failure isolation.
- `validate_document()` and `persist_document()` are independent boundaries and
  should remain outside provider adapters and run-state code.
- `ModelErrorKind` is the starting point for retry selection. Phase 3 should add
  structured attempt/status/retryability data without parsing human-readable
  error messages.
- Progress and configuration callbacks keep library code independent from CLI
  rendering. Phase 3 should preserve that separation when state updates become
  concurrent.

## Technical Debt

- `ariadne.generation` contains planning, context selection, prompting,
  provenance, validation, persistence, and orchestration in one module. The
  functions are individually small, but Phase 3 should separate the
  per-module executor from run coordination before adding concurrency.
- First-run configuration initialization performs one discovery pass to locate
  the repository and a second pass after writing configuration. This is simple
  and safe but duplicates work once per repository.
- Context selection uses static filename priorities and may include descendant
  source files in an ancestor module until the budget is exhausted. It is
  deterministic but not semantic ranking.
- Prompt budgeting reserves a fixed estimated overhead and does not count the
  final serialized prompt with a provider tokenizer.
- Markdown validation is intentionally structural and does not detect all
  malformed Markdown, invalid links, path escapes, or unsupported protocol
  leakage.
- The OpenAI-compatible adapter uses synchronous `urllib`. Phase 3 plans an
  async provider interface and should use an established async HTTP client
  unless an SDK compatibility spike demonstrates clear value.
- `ModelRequest` currently contains only system and user text. Tool definitions
  and multi-turn/tool-call state remain deliberately absent until retrieval.
- Configuration and generation errors are readable but not yet structured for
  persistent retry decisions.
- Test coverage is broad but no numeric coverage threshold is collected.

None of this debt requires another Phase 2 feature. The parent-document setting
and planned-parent relationship were corrected during close-out so Phase 3 can
rely on them directly.

## Testing Status

The model-independent suite covers:

- configuration discovery, initialization, defaults, validation, and headers;
- Phase 1 root resolution, scanning, filtering, logical modules, collapsing,
  inspect rendering, and fixture integrations;
- top-down and module-only planning, naming, and collision disambiguation;
- context evidence labels, generated-document treatment, parent-document
  configuration, leaf guidance, and prompt construction;
- OpenAI-compatible/vLLM-style payloads, structured response content, sanitized
  diagnostics, and model error classification;
- provenance composition and front-matter/title validation;
- overwrite protection and atomic persistence;
- subtree generation, parent context propagation, progress events, CLI output,
  and model-error guidance using fake backends.

Tests require no network service. One directory-symlink test may skip on Windows
hosts where creating symlinks is unavailable. No numeric coverage metric is
currently collected.

## Recommendations for Phase 3

1. Preserve `InspectionResult`, `PlannedModule`, context assembly, validation,
   and persistence as stable Phase 2 boundaries.
2. Extract the per-module execution body from `weave_repository()` before
   introducing an async dependency scheduler.
3. Add persistent state transitions around module attempts; do not place run
   state on immutable discovery models.
4. Evolve `LLMBackend.generate()` to an async provider-neutral operation after
   the documented SDK-versus-generic-client compatibility spike.
5. Keep `max_concurrency` at `1` by default and release children after their
   parent attempt finishes, even when that attempt fails.
6. Serialize or otherwise coordinate state/progress updates while retaining
   deterministic final ordering.
7. Extend model failures with structured retry and attempt metadata, then add
   retry and context-overflow policies outside individual provider calls.
8. Preserve atomic writes and human-modification protection across retry,
   resume, cancellation, and cleanup.
