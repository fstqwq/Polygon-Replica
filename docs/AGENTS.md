# Documentation contract

Documentation describes the current system and is organized by ownership:

- `design/` explains boundaries and relationships.
- `protocol/` owns persisted and exchanged shapes, identities, lifecycle, and
  failure semantics.
- `operations/` owns deployment and operator behavior.
- `src/` mirrors the actual application package responsibilities.
- `module-taxonomy.md` owns stable application layers and module-role naming.
- `coding-style.md` owns Python authoring and import rules.
- `testing.md` owns test selection and assertion policy.

Every design and behavior statement directly states a current fact: the acting
component, its action, and the applicable scope. Delete contrastive framing,
rejected alternatives, obsolete behavior, and hypothetical caveats.

Each fact has one home. Other documents link to its owner instead of copying it.
Use `MUST` and `MUST NOT` only for external protocols and security or storage
invariants. Describe internal Python structure and current orchestration in the
present tense.

When implementation and documentation differ, inspect the implementation and
the relevant external contract. Fix a current-contract defect; do not invent a
future target to make the implementation look incomplete. Changes to a route,
exchanged JSON shape, persisted locator, cache identity, or cleanup rule update
the owning protocol in the same change.

Package documentation should explain responsibility, inputs, outputs,
dependencies, and lifecycle. It should not repeat route lists, SQL inventories,
or line-by-line source structure.
