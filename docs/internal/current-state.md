# Ariadne Current State

Phase 2 (LLM Integration and Documentation Generation) is complete. Ariadne
can inspect repositories as in Phase 1 and can now generate module-level
Markdown through an OpenAI-compatible model endpoint.

## Current Architecture

Discovery remains deterministic and model-independent:

1. `ariadne.config` loads typed repository, module, model, context, and
   generation settings from `.ariadne/config.yaml`.
2. `ariadne.inspection.inspect_repository()` resolves and scans the selected
   repository subtree and constructs the immutable logical module tree.
3. `ariadne.generation` plans a stateless top-down traversal, assembles bounded
   context, prompts the model, adds deterministic provenance, validates the
   result, and persists it atomically.
4. `ariadne.llm` defines the provider-neutral request/response protocol and the
   OpenAI-compatible chat-completions reference backend.
5. `ariadne.cli` exposes both `ariadne inspect` and `ariadne weave`.

Discovery models remain repository-relative and immutable. The model backend
receives only assembled text; it has no filesystem access and no retrieval
tools.

Inspection reports the number of logical modules, which is also the number of
documentation files planned for a subtree weave.

## Model Abstraction

`LLMBackend.generate()` accepts separate system and user prompts and returns
generated text plus the serving model name. The included
`OpenAICompatibleBackend` sends `/v1/chat/completions` requests with configured
model, output-token limit, temperature, timeout, and arbitrary string headers.
It classifies authentication, timeout, connection, rate-limit, context-length,
server, and malformed-response failures.

The payload intentionally uses the common OpenAI chat-completions shape used by
vLLM OpenAI-compatible servers. Tests mock the HTTP transport; no running model
is required.

The adapter accepts both string content and structured text-part content.
Incompatible responses identify the expected
`choices[0].message.content` shape without echoing response bodies or endpoint
query parameters. It uses Python's standard HTTP library to keep the core
dependency-light and avoid coupling the provider-neutral interface to one
vendor SDK. A future SDK-backed provider can implement the same `LLMBackend`
contract when its response normalization or authentication support provides a
concrete benefit.

## Context and Evidence Policy

The Context Assembler supplies repository identity and commit, module location,
ancestors and children, a bounded directory tree, selected file contents,
context omissions, and available hierarchical documentation.

Evidence is labeled in the prompt:

- source, manifests, and configuration are primary evidence;
- human-authored documentation is secondary evidence;
- prior `*-genai-doc.md` documents are unverified, potentially stale secondary
  evidence.

Generated documents are excluded from physical source scanning so they cannot
change module boundaries, language totals, or source-size totals. When enabled,
the assembler reads relevant prior generated documents separately and instructs
the model never to prefer them over source evidence.

File selection is deterministic. Manifests and configuration lead entry points,
source, human documentation, tests, miscellaneous text, and finally generated
documentation. Binary files are omitted, individual files are size-bounded,
and a stable character-to-token estimate bounds the overall initial prompt.
Truncations and omissions are disclosed to the model.

For leaf modules, prompt construction asks the model to consider additional
evidence-grounded detail about sibling files in the leaf, including how they
divide responsibilities and collaborate, without reverting to file-by-file
paraphrase.

## Documentation Pipeline

`ariadne weave [path]` runs:

```text
Discover -> Plan Top-Down -> Assemble Context -> Invoke Model
         -> Add Provenance -> Validate -> Persist Atomically
```

The selected module and descendants are generated sequentially by default.
`--module-only` generates only the selection. A completed parent document is
available to its children.

The orchestration layer emits initial and per-module progress events. The CLI
renders these as progress bars on stderr while reserving stdout for generated
document paths.

If no configuration is discoverable, the first weave creates
`.ariadne/config.yaml`, adds `.ariadne/` to the repository `.gitignore`, and
reports that the generated model settings should be reviewed. Model failures
report the sanitized endpoint, configuration path, and corrective checks rather
than only reporting a generic connection failure.

Each module receives one deterministic filesystem-safe `*-genai-doc.md` file in
its physical directory. Collisions are disambiguated before model invocation.
The harness, rather than the model, adds YAML front matter and the visible
AI-generated disclaimer. Validation requires complete provenance, exactly one
level-one title, and nonempty Markdown. Temporary files are flushed and
atomically replaced only after validation.

Existing Ariadne-generated documents may be replaced. Documents marked
human-reviewed or human-modified, and files without Ariadne provenance, are
protected unless `--force` is supplied.

## Configuration

Phase 2 recognizes these sections in addition to Phase 1 settings:

```yaml
model:
  provider: openai-compatible
  model: local-model
  endpoint: http://localhost:8000/v1
  context_window: 32768
  max_output_tokens: 6000
  temperature: 0.2
  timeout_seconds: 300
  headers: {}

context:
  max_initial_tokens: 24000
  max_file_bytes: 100000
  max_tree_depth: 3
  include_parent_docs: true
  include_generated_docs: true
  characters_per_token: 4.0

generation:
  output_suffix: -genai-doc.md
  include_front_matter: true
  atomic_writes: true
  overwrite_generated: true
  overwrite_human_modified: false
```

Headers are passed to the configured endpoint and should be handled as secrets
when they contain credentials. Ariadne does not print header values in model
errors.

## Deferred Features

The following remain intentionally outside Phase 2:

- retries, failure isolation, persistent run state, resume, and partial drafts;
- retrieval tools, search, and model tool-call loops;
- child-document refinement, bottom-up traversal, and a second pass;
- AST or dependency indexing;
- Mermaid diagrams, navigation refinement, and a documentation website;
- incremental regeneration, stale detection, and advanced source verification.

Generation is sequential and stops at the first failed module. Token counts are
estimated rather than calculated with a provider tokenizer.

## Recommendations for Phase 3

1. Wrap the existing per-module pipeline with persistent run records rather
   than adding state to discovery or context models.
2. Record planned output paths, prompt-size estimates, model error kinds, and
   atomic completion status before adding retry selection.
3. Isolate module failures while retaining deterministic traversal order.
4. Add retry and context-overflow policies around `LLMBackend.generate()`;
   keep the backend itself single-attempt.
5. Preserve human-modification protection during resume and cleanup.

## Testing Status

The suite covers all Phase 1 discovery behavior plus Phase 2 configuration,
chat-completions/vLLM-compatible serialization, model error classification,
context evidence and budgeting, prompt construction, traversal, naming and
collision handling, provenance, validation, overwrite protection, atomic
persistence, and fake-model CLI generation. Tests require no network service.
