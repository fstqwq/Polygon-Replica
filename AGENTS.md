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
- Boundary code validates once; internal code consumes canonical typed values.
- Do not preserve removed project-owned shapes through compatibility layers.
- Do not predeclare project-owned schema/format/implementation versions for a
  hypothetical fork. Add an explicit exchanged or persisted field only when a
  real compatibility boundary exists.
- Do not hide compatibility identity in constants, cache-key salts, variable
  names, or other hard-coded markers. Externally required protocol version
  fields remain valid.
- Product and protocol documents define behavior; tests provide selected
  executable evidence and do not create a contract. Follow
  [docs/testing.md](docs/testing.md).
- Python imports follow the layout and dependency rules in
  [docs/coding-style.md](docs/coding-style.md).
- Do not run tests on Windows. Ask which Linux environment and virtualenv to use
  before running tests.
- Preserve unrelated worktree changes. Do not commit or push unless requested.

Documentation conventions are in [docs/AGENTS.md](docs/AGENTS.md).
