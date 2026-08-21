# AGENTS.md

Polygon-Replica is a local problem-authoring and judging system. Start at
[docs/README.md](docs/README.md), then read only the owning protocol and package
map pages for the area being changed.

Repository rules:

- Git is authoritative for committed problem sources; SQLite stores metadata;
  derived payloads live in their configured filesystem roots.
- Document current behavior. Put each fact in one owning document and link to it
  elsewhere.
- External wire/storage contracts and security invariants use normative wording;
  internal implementation notes use descriptive present tense.
- Product and protocol documents define behavior; tests provide selected
  executable evidence and do not create a contract. Follow
  [docs/testing.md](docs/testing.md).
- Python imports follow the layout and dependency rules in
  [docs/coding-style.md](docs/coding-style.md).
- Application layers and module responsibilities follow
  [docs/module-taxonomy.md](docs/module-taxonomy.md).
- Preserve unrelated worktree changes. Do not commit or push unless requested.

## Environment discipline

- Run tests only on Linux.
- If a command fails because of the environment or missing dependencies, first
  assume that the selected interpreter or virtualenv is incorrect.
- Before running tests or switching environments, ask which Linux environment
  and virtualenv to use.
- Do not silently switch to WSL, another Python interpreter, another
  virtualenv, a remote host, or another machine.

## Python code style

- Use clear type hints everywhere.
- Reduce runtime type checks by strengthening upstream types and boundaries
  first.
- Prefer canonical internal shapes over repeated `isinstance`, `is not None`,
  truthiness, and similar defensive branches.
- Boundary code may validate and normalize external input once. Internal code
  consumes canonical shapes directly.
- Once a token is canonical inside the system, do not keep normalizing it with
  `.strip()`, `.lower()`, `str(...)`, or similar compatibility coercion.

## Refactoring

Do not maintain backward compatibility for removed project-owned behavior or
data shapes. Prefer deletion and a unified current model:

- Remove code that is not needed.
- Refactor code that is needed and can be improved.
- Keep code as-is only when it is needed and cannot be improved safely within
  the task.
- Never use a local patchwork compatibility layer when one unified model is
  possible.
- Do not pre-design compatibility machinery for hypothetical future forks.
  Model only the current canonical shape.
- Do not invent project-owned schema, format, materializer, converter, or
  implementation version numbers before a concrete compatibility boundary
  exists.
- Do not hide compatibility identity in variables, constants, cache-key salts,
  or other hard-coded markers. Externally required protocol and file-format
  version fields remain valid.
- If a real hard fork becomes necessary, add an explicit persisted or exchanged
  field together with the fork behavior at that time. Do not reserve fields in
  advance.
- For files larger than 1000 lines, consider splitting them.
- Use subdirectories or deeper module trees when they improve responsibility
  boundaries.
- Define responsibility boundaries and invariants before a refactor. Reject a
  split whose boundary or invariant cannot be stated clearly.

Documentation conventions are in [docs/AGENTS.md](docs/AGENTS.md).
