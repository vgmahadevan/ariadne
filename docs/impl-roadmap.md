# Ariadne — Implementation Roadmap

This document summarizes the implementation milestones. The full specification remains the authoritative reference.

---

# Overall Goal

Generate a navigable documentation corpus for a large repository by documenting one logical module at a time using bounded context and limited repository retrieval.

The implementation should remain incremental. Finish the current milestone before beginning the next.

---

# Phase 1 — Repository Discovery

Implement:

* repository root detection
* repository scanning
* Git / ignore filtering
* logical module discovery
* structural-directory collapsing
* `ariadne inspect`

Do **not** implement:

* LLM integration
* documentation generation
* retrieval tools
* retry
* diagrams
* website
* AST indexing

Acceptance criteria:

* meaningful logical module tree
* collapsed structural directories
* fixture-based tests
* inspect command displays planned documentation hierarchy

---

# Phase 2 — Documentation Generation

Implement:

* model abstraction
* context assembler
* top-down traversal
* module-level documentation
* provenance header
* deterministic filenames
* atomic writes

One generated document per logical module.

No file-level documentation.

---

# Phase 3 — Robustness

Implement:

* persistent run state
* module failure isolation
* partial draft handling
* retries
* resume
* safe cleanup
* context-overflow handling
* async model invocation
* bounded parallel generation across eligible modules
* configurable concurrency with a sequential default
* deterministic progress and result reporting despite out-of-order completion
* graceful cancellation of in-flight work

A failed module must never stop the overall weave.

Top-down context remains ordered by ancestry: a module becomes eligible after
its parent attempt finishes. If the parent fails, descendants still run without
new parent documentation and record that omission in their context. Siblings
and independent branches may run concurrently.

Before selecting an LLM client dependency, evaluate an async OpenAI-compatible
SDK against a thin adapter over an established generic async HTTP client. The
generic adapter is the baseline direction because Ariadne currently needs only
a small request and response surface. The provider-neutral Ariadne interface
must not expose SDK-specific types. Adopt an SDK only if it demonstrably
improves vLLM compatibility, response normalization, authentication, and error
handling without introducing hidden retry or provider coupling.

---

# Phase 4 — Repository Retrieval

Add bounded retrieval:

* list_directory
* read_file
* search_code
* get_module_tree

Limit tool calls and detect loops.

The harness—not the model—controls traversal.

---

# Phase 5 — Hierarchical Refinement

Implement:

* bottom-up traversal
* optional second pass
* parent/child documentation refinement
* cross-links
* repository index improvements

---

# Phase 6 — Documentation Enhancements

Implement optional:

* Mermaid diagrams
* documentation website
* search
* rendered hierarchy

The Markdown documentation corpus remains the source of truth.

---

# Phase 7 — Incremental Updates

Implement:

* source fingerprints
* stale detection
* incremental regeneration
* ancestor propagation
* verification

---

# Phase 8 — Advanced Intelligence

Optional features:

* AST / code graph
* dependency-aware retrieval
* consistency checking
* documentation coverage metrics
* advanced verification

---

# Design Rules

* Accuracy over completeness.
* Explicit uncertainty over hallucination.
* One logical module at a time.
* Small coherent commits.
* Preserve extension points, but do not implement future features early.
* Every advanced feature should be independently configurable.
