# State derivation and lifecycle

Polygon Replica keeps authored source separate from the results produced from it. State moves in one direction:

```text
new problem or imported package
              |
              v
     per-user workspace --------> statement preview or workspace verification
              |
              | publish
              v
     published revision
              |
              v
        package export
              |
              +---- full verification by default
              |     or standard-solution-only
              v
   native package (certified or not verified)
              |
              v
      external packages
```

Generated tests, answers, PDFs, logs, and packages never become authored source. An imported external package becomes workspace source and follows the same publication path as a new problem.

## State classes

- **Source** is problem Git history, user workspaces, contest source, and attachments. It is durable and its database identity must match its filesystem content.
- **Derived** data is delivery output such as native and external packages. It survives restart and is removed by generated-data cleanup together with its metadata.
- **Cache** is disposable execution and preview data. It may be missing, is never authoritative, and is cleared at startup.

SQLite owns identities, relationships, lifecycle states, and filesystem locators. Large payloads remain in configured roots; a source or derived locator must resolve to its recorded payload.

## Lifecycle

| State | Derived from | Fixed at | Lifetime |
| --- | --- | --- | --- |
| Workspace | New problem, imported source, or a published revision | Not fixed; its owner edits it | Durable until deleted |
| Published revision | Reviewed workspace | Publish | Durable Git history |
| Statement preview | Workspace or native package source plus language/output | Preview request | Cache; invalidated at startup/deploy |
| Workspace verification | Frozen workspace snapshot and selected targets | Verification admission | Durable summary and decisions; execution payloads are cache |
| Native package | Published revision, generated inputs, and main-correct answers | Package export | Derived; certified by full verification or marked not verified |
| External package | Native package and target adapter | Adapter run | Derived and reusable for that native package/format |
| Contest package download | Current roster and each current native package | Download request | Temporary response; deleted after transfer |

## Authoring and publication

Each user edits an isolated workspace. Verification freezes a snapshot, and publication records a new Git revision after resolving conflicts with the current published revision. Verification never publishes source, and later edits or publications never rewrite earlier results.

## Verification

A verification follows `queued -> running -> ok | failed | cancelled`. Its task decisions are durable; execution payloads are cache. Restart fails interrupted work and clears process-local coordination, leases, queues, and cache payloads.

## Native and external packages

Package export creates or reuses a native package for the current published revision. Full verification certifies matching evidence; standard-solution-only export produces the same archive marked `not verified`. External packages derive from that native package. The [package protocol](../protocol/package.md) defines reuse, certification, and adapter output.

## Contest package download

Contest roster, indices, statement source, and attachments are durable authoring state. `idx` is both display identity and canonical order. A contest download freezes the ready native packages, prepares or reuses their external packages, and returns a temporary outer bundle in that order.

## Invalidation and cleanup

| Event | What changes | What remains |
| --- | --- | --- |
| Edit a workspace | Workspace content and its verification signature | Published revisions, other workspaces, packages, contest downloads |
| Publish | Current published revision | Earlier revisions and their derived products |
| Remove access | The next authorization decision | Source and derived bytes |
| Restart | Active jobs fail; runtime queues, leases, and cache clear | Git, workspaces, users, contests, and durable contest source |
| Missing or corrupt native package | Package becomes unavailable; cached external packages are invalidated | Its published revision |
| Generated-data cleanup | Verification, package, export, and preview rows and payloads are removed | Git, workspaces, users, problem metadata, contests, contest source, backups |

Detailed transitions and data shapes are defined by the [problem source](../protocol/problem-source.md), [execution](../protocol/execution.md), [package](../protocol/package.md), and [storage](../protocol/storage.md) protocols.
