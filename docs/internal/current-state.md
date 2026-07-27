# Ariadne Current State

Phase 1 (Repository Discovery) is complete. Ariadne currently provides a
deterministic repository inspection pipeline and no documentation-generation
capabilities.

## Current Architecture

The Python package uses the conventional `src/ariadne` layout. Its components
are deliberately small and communicate through dataclasses defined in
`ariadne.models`.

1. `ariadne.config` discovers and validates `.ariadne/config.yaml`.
2. `ariadne.repository` resolves the repository root and selected subtree.
3. `ariadne.git` obtains tracked, untracked, and ignored paths when Git is
   available.
4. `ariadne.scanner` walks the selected tree and applies symlink, default
   ignore, configured pattern, `.gitignore`, and Git-policy filters. It returns
   physical nodes plus ignored-path records.
5. `ariadne.modules` converts eligible physical nodes into an immutable logical
   module tree, prunes empty directories, collapses structural chains, and
   aggregates source sizes and languages.
6. `ariadne.inspection.inspect_repository` orchestrates discovery and returns
   an `InspectionResult`.
7. `ariadne.render` and `ariadne.cli` present that result through
   `ariadne inspect`.

Filesystem paths inside discovery models are repository-relative POSIX strings.
Native absolute `Path` values are retained in `RepositoryContext` for the root
and selected subtree.

## Implemented Features

- Repository-root precedence: explicit `--root`, configured root, nearest Git
  root, then current working directory.
- Safe validation of an optional inspection subtree.
- Deterministic physical scanning of directories and files.
- File size, extension, language, Git status, manifest, and human-documentation
  metadata.
- Conservative default ignores and configurable additional/default,
  include, and exclude patterns.
- Optional `.gitignore` handling.
- `tracked-only`, `tracked-and-untracked`, and `all-nonignored` policies, with a
  Git-independent fallback.
- Symlink exclusion without following links or allowing external targets to
  escape repository-relative path handling.
- Logical repository and directory modules with deterministic ordering.
- Manifest-aware module boundaries and polyglot language aggregation.
- Conservative unary structural-directory collapsing with retained physical
  paths and collapsed segments.
- `ariadne inspect [path]`, `--config`, `--root`, `--no-git`,
  `--tracked-only`, and `--include-untracked`.
- Three inspect display levels:
  - default: module-name tree;
  - `-v`: module physical paths and ignored directories;
  - `-vv`: repository metadata, collapsed paths, language/source-size
    summaries, and all ignored paths.

## Deferred Features

The following are intentionally outside Phase 1:

- model-provider and LLM integration;
- context assembly and source-content selection;
- documentation generation, validation, naming, and atomic persistence;
- traversal execution beyond displaying the deterministic module hierarchy;
- provenance headers and generated-document navigation;
- run state, failure isolation, retry, resume, and cleanup;
- bounded retrieval tools;
- bottom-up refinement and cross-linking;
- Mermaid diagrams, website generation, search, and coverage views;
- AST/code-graph indexing;
- incremental generation, fingerprints, stale detection, and verification.

## Important Extension Points

- Phase 2 should continue using `inspect_repository()` as the discovery
  boundary. `InspectionResult.root_module` supplies the logical hierarchy, and
  `InspectionResult.physical_nodes` supplies the repository-relative files from
  which context can be selected.
- A traversal planner should consume `LogicalModule` without adding traversal
  state to the immutable discovery model. Top-down ordering can be implemented
  as a separate iterator or service.
- Context assembly should map a logical module's physical path to eligible
  `PhysicalNode` records, then perform content classification and budgeting.
  File reading does not belong in the scanner.
- Model invocation should sit behind a provider-neutral interface after context
  assembly. Discovery and inspect must remain usable without a model.
- Generation naming, validation, and atomic persistence should be separate
  services called by the future `weave` orchestration command.
- Persistent state and retry belong around the generation pipeline in Phase 3,
  not inside repository discovery.
- Retrieval tools in Phase 4 should use the resolved repository context and
  inspection models so path safety and filtering rules remain centralized.

## Technical Debt

- Structural collapsing is intentionally conservative: any direct file stops a
  collapse. It does not attempt semantic detection of API or deployment
  boundaries.
- Manifest and language detection use fixed filename/extension registries.
  Additional ecosystems should be added only when supported by concrete
  fixtures.
- Ignored Git directory matching uses a linear prefix check. Very large ignored
  sets may eventually justify a prefix index, but current behavior is simple and
  deterministic.
- The scanner records excluded symlinks as ignored paths rather than physical
  nodes. Configurable internal-link traversal and cycle detection remain
  unimplemented.
- Binary/large-file classification and generated-document markers are not yet
  represented. They should be added with Phase 2 context selection, before any
  source content is sent to a model.
- High-verbosity inspect does not yet show documentation filenames, token
  estimates, or an explicit traversal-order section. Filenames and token
  estimates depend on Phase 2 policies; current child ordering already provides
  a deterministic planned hierarchy.
- Pattern behavior is covered for core precedence cases, but nested
  `.gitignore` edge cases and a broader cross-platform path matrix need more
  coverage.

## Testing Status

The test suite is model-independent and currently covers:

- configuration discovery, parsing, defaults, and validation;
- root precedence, Git-root discovery, subtree selection, and path rejection;
- default/configured ignores, include/exclude precedence, language metadata,
  Git policies, optional `.gitignore`, and external symlink exclusion;
- logical aggregation, branching/manifest boundaries, structural collapsing,
  and disabled collapsing;
- CLI usage, errors, and all inspect verbosity levels;
- deterministic integration behavior using Python, nested Java, JavaScript
  monorepo, and polyglot service fixtures, including untracked files.

The suite does not currently collect a numeric coverage metric. Rust workspace,
existing generated-document, nested `.gitignore`, and large/binary-file
fixtures are not yet present.

## Recommendations for Phase 2

1. Keep discovery immutable and introduce a separate deterministic top-down
   traversal planner.
2. Add generated-document markers and binary/large-text classification before
   implementing context assembly.
3. Define output naming and collision behavior before writing generation code.
4. Implement context assembly, model abstraction, draft validation, and atomic
   persistence as separate services coordinated by `ariadne weave`.
5. Reuse physical-node filtering and repository-relative paths for every file
   read; do not let model providers access the filesystem directly.
6. Add focused tests for each pipeline stage and one model-stub integration
   fixture before connecting a real provider.
