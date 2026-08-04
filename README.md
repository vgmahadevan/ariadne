# Ariadne

*"Safe, then, and in high honor, he reversed himself. 
He guided his wandering steps with Ariadne's thin thread 
so that, as he left labyrinthine bends, invisible deception would not delude him."* - Catullus, Poem 64 (112-115)

Ariadne is a CLI tool that weaves a tapestry of documentation across a labyrinthine codebase. Specifically, it is a harness to help LLMs with relatively small context windows generate documentation in large codebases; e.g., in air-gapped environments with access only to local LLMs.

## Commands

Inspect the deterministic logical module hierarchy without invoking a model:

```bash
ariadne inspect [path]
```

The inspection output includes the number of module documentation files that a
subsequent subtree weave will generate. The logical module at the repository
root is named `repository`; selected subtree roots retain their module name.

Generate one `*-genai-doc.md` per module in a selected subtree:

```bash
ariadne weave [path]
```

During a regular weave, each generated document records `ariadne.api: true` or
`false` in its YAML metadata. To generate detailed route and parameter
documentation only for modules marked as APIs, run:

```console
ariadne weave [path] --api
```

API documents use the separate `*-genai-api-doc.md` suffix. Run a regular weave
first (or regenerate stale module documents) so the API selection metadata is
available.

While weaving, Ariadne writes module progress bars to stderr and keeps generated
document paths on stdout for scripting. A weave continues after individual
module failures and exits with status 1 after printing its complete summary.
The progress bar updates in place and reports elapsed and estimated remaining
time; the final summary includes total elapsed time.

Interrupted or partially failed runs can be reconciled and resumed:

```bash
ariadne weave [path] --resume
```

Independent eligible branches can run concurrently. The default allows up to
eight active model requests:

```bash
ariadne weave [path] --max-concurrency 4
```

Safely preview or remove Ariadne-owned documents without touching unrelated
Markdown:

```bash
ariadne clean [path] --dry-run
ariadne clean [path]
```

Use `ariadne clean [path] --api` to remove only API documents. It leaves regular
module documents intact; `--api --drafts` likewise selects only API-weave drafts.

Human-reviewed or modified generated documents are retained unless
`--include-human-modified` is supplied. `--drafts` also selects partial draft
artifacts while preserving run history.

Use `--module-only` to generate only the selected module. Ariadne uses an
OpenAI-compatible `/v1/chat/completions` endpoint configured in
`.ariadne/config.yaml`:

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
generation:
  max_concurrency: 8
retrieval:
  enabled: true
  tools:
    - list_directory
    - read_file
    - search_code
    - get_module_tree
  max_tool_calls_per_module: 20
  max_identical_calls: 2
  max_result_bytes: 50000
  tool_timeout_seconds: 30
```

The same request shape is compatible with common vLLM OpenAI servers. Generated
documents include machine-readable provenance and are written atomically.
Existing documents marked as human-reviewed or human-modified are protected
unless `--force` is supplied.

When retrieval is enabled, the model can inspect admissible files anywhere
under the configured repository root, including files outside the selected
weave subtree. Repository file policy, ignore rules, path containment, result
limits, timeouts, and loop detection remain controlled by Ariadne.

On the first `weave` in a repository without configuration, Ariadne creates
`.ariadne/config.yaml`, adds `.ariadne/` to `.gitignore`, and reports the new
configuration path. Review the generated model name and endpoint before
retrying if no compatible service is already running at the default
`http://localhost:8000/v1` endpoint.

The `.ariadne/` directory also contains a generated `README.md` explaining its
contents:

- `config.yaml` contains user-editable settings.
- `state.json` points to the latest weave.
- `runs/` contains resumable run manifests and module outcomes.
- `drafts/` contains partial Markdown retained after validation failures.

Run state is separate from the generated `*-genai-doc.md` corpus and is
preserved by `ariadne clean`.

