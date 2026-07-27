# AGENTS.md

# Ariadne

This repository implements **Ariadne**, a hierarchical documentation generator for large codebases.

## Guiding Principle

Ariadne should behave like a disciplined technical writer with access to a code browser—not an autonomous software engineer.

Favor deterministic infrastructure over autonomous behavior.

## Before You Code

* Read the current milestone before making changes.
* Search for existing patterns before introducing new abstractions.
* Keep changes localized.
* Understand nearby code before editing it.

## Scope

* Implement **only the current milestone**.
* Do not implement future phases or placeholder frameworks.
* Do not perform unrelated refactors.
* Do not change public behavior unless the task requires it.
* Ask before making destructive or repository-wide changes.

## Design Philosophy

* Keep the core simple. Implementations should remain lean.
* Avoid overengineering unless there are compelling downstream benefits.
* Prefer composition over premature abstraction.
* Build extension points only where immediately useful.
* Every optional feature must be independently disableable.

## Code Quality

* Match existing project style and conventions.
* Avoid unnecessary dependencies.
* Prefer small, understandable implementations.
* Remove dead code rather than leaving unused scaffolding.

## Testing

Run the smallest relevant test suite first.

For every completed task:

* add or update tests for changed behavior;
* report exactly which tests were run;
* report any tests that could not be run and why.

Do not weaken tests simply to make them pass.

## Validation

Before finishing:

* inspect the final diff;
* ensure no unrelated files were modified;
* remove debug code or temporary artifacts;
* report assumptions, limitations, and remaining work.

## Repository Documentation

`docs/ariadne-spec.md` is the authoritative design reference.

Consult only the sections relevant to the current milestone instead of treating the entire specification as an implementation prompt.

The implementation should evolve incrementally.
