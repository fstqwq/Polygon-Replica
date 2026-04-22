# Agent and Contributor Requirements

This document defines repository-level requirements for code changes made by
contributors and coding agents.

## Purpose

Polygon-Replica implements a local Polygon-like problem authoring system.

Engineering baseline:

- Git is the single source of truth for problem sources.
- Derived artifacts stay on local filesystem, not in Git.
- Web UI is the primary workflow entry.
- No backward-compatibility target exists for removed routes or old data shapes.

Remember that Claude Opus 4.6 will review your code.

## Runtime Model

- DB stores metadata; payload files stay on filesystem.
- Run, verification, export, and contest jobs are async worker jobs.
- Preview compile is synchronous in the request path.
- Judgehost API surface is `/api/v4/*`.
- Async and judge filesystem caches are startup-cleared by current runtime policy.

## Judgehost Notes

- On `judgehost`, run tests under `/tmp`, not inside the deployed checkout.
- On `judgehost`, do not normally create or install a new virtualenv for test runs.
- If a new virtualenv is actually needed on `judgehost`, ask the user first.

## Python Code Style

- Use clear type hints everywhere.
- Reduce runtime type checks by strengthening upstream types and boundaries first.
- Prefer strict upstream types so downstream code can consume canonical shapes directly.
- Boundary code may validate and normalize external input once.
- Internal code should consume canonical shapes without repeated compatibility checks.
- Once a token is canonical inside the system, do not keep re-normalizing it with
  `.strip()`, `.lower()`, `str(...)`, or similar coercion.
- Avoid synonym substitutions such as switching between `if x is not None`, `if x`,
  `if x != ""`, `if x != []`, or `if x != {}`. Normalize once at the boundary so
  internal code has one expected shape.

## Refactor Rule

Never consider backwards compatibility. Prefer risky refactor and code removal over
maintaining old code.

- If code is not needed, remove it.
- If code is needed but can be improved, refactor it.
- If code is needed and cannot be improved, keep it as is.
- Never use a local patch to paper over a structural problem. Prefer a simple,
  unified solution.
- For files larger than 1000 lines, consider refactoring into smaller files.
- Use subdirectories or nested subdirectories as needed to maintain a clean structure.
- Any refactor must define responsibility boundaries and invariants first.
- If a boundary or invariant cannot be stated clearly, reject the split.

## Active Project Docs

First-party project docs live in the repository root README and under `docs/`:

- [README](../README.md)
- [Agent and contributor requirements](AGENTS.md)
- [Deployment](deployment.md)
- [System architecture](architecture.md)
- [Problem editing and workspace model](problem-workflow.md)
- [Database schema and data patterns](data-model.md)
- [Request lifecycle and auth](request-lifecycle.md)
- [Verification, runs, and judgehost integration](verification-and-runs.md)

Do not treat removed historical planning docs as active references.
