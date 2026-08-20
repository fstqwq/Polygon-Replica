# State derivation and lifecycle

Polygon Replica separates authored source from the results produced from that
source. State moves in one direction:

```text
new problem or imported package
              |
              v
     per-user workspace --------> preview or workspace verification
              |
              | publish
              v
   official problem version
              +---- full verification ----> Native Package
                                                     |
                                   +-----------------+------------------+
                                   |                                    |
                                   v                                    v
                     external-package adapters                    Contest outputs
                                   |
                                   v
                 DOMjudge / ICPC 2025-09 / QOJ / Nowcoder packages
```

Generated tests, official answers, PDFs, logs, and packages never flow back
into authored problem source. Import converts an external package into
workspace source, which then follows the same authoring and publication flow as
a problem created in Polygon Replica.

Persisted files belong to one of three classes:

- **Source** is authored state: problem Git history, user workspaces, and
  Contest source and attachments. Source is durable, and its database identity
  and filesystem content must correspond.
- **Derived** data is generated delivery products such as Native Packages,
  external packages, and Contest outputs. They survive restart. Their
  database record and filesystem payload describe one object; a missing or
  mismatched payload is an integrity failure. Maintenance cleanup removes both.
- **Cache** is disposable execution and preview data. Cache entries may be
  missing, are never a source of truth, and are cleared at application startup.

## State and derivation

| State | Derived from | Fixed at | Lifetime |
| --- | --- | --- | --- |
| Workspace | A new problem, imported source, or an existing official version | Not fixed; its owner continues editing it | Durable until explicitly deleted |
| Official problem version | A reviewed workspace published to the problem | Publish | Durable Git history |
| Statement Preview | A Workspace or Native Package statement render source plus requested language/output | Preview request | Cache; all entries are invalidated at startup/deploy |
| Workspace verification | One workspace snapshot and selected verification targets | Verification admission and activation | Cleanup-safe database record; program input, output, answer, feedback, transcript, and logs are cache |
| Native Package | One official problem version and a successful full verification of that exact source | Package Export verification phase | Derived; directly downloadable and reusable while its source snapshot and verified test data remain intact |
| External package | One Native Package plus a target adapter; standalone DOMjudge exports also use the canonical problem slug | External-package adapter run | Derived; reusable for the same Native Package and format |
| Contest package download | Current Contest roster plus every problem's current Native Package | Download request | Transient response; deleted after transfer |

SQLite records identities, relationships, lifecycle states, and filesystem
locators. Large payloads live in their configured filesystem roots. Source and
derived locators must resolve to the payload owned by their database record.
Cache locators may become unavailable without invalidating the durable summary
that refers to them.

## Authoring and publication

Each user edits a separate workspace. Workspace changes do not affect another
user's workspace or the official problem version. A verification takes a frozen
snapshot of the selected workspace, so later edits do not change a running or
completed result.

Publishing reconciles the workspace with the current official version and then
records a new official version. A conflict must be resolved before publication.
Publication does not claim that the new version passed verification, and a
successful workspace verification does not publish source.

An edit after verification changes the workspace's verification signature. The
old result remains an account of the snapshot it actually tested, but the UI
marks it stale for the changed workspace. Publishing a newer official version
does not rewrite older versions, verifications, or packages.

## Verification

Verification moves through this terminal lifecycle:

```text
queued -> running -> ok | failed | cancelled
```

Activation stores the complete task graph once. Generators produce testcase
inputs; the accepted solution produces official answers; other solutions are
checked against those same inputs and answers. The task results and their
cache locators belong to that verification and are not mutable evidence for a
later source snapshot. Program input, output, answer, feedback, transcript,
compile log, and execution log payloads are all cache.

A process restart cannot resume the in-memory batch runtime, Judgehost leases, or
worker queue. Startup marks interrupted work failed and clears verification
cache. Completed result rows may remain for display, while their downloadable
program input, output, answer, feedback, transcript, and logs become
unavailable.

## Native Package and external-package lifecycle

A Package Export request freezes the current official problem version. It never
reads a user's changing workspace. For that version:

1. An existing Native Package is checksum-checked and reused.
2. If none exists, the service extracts the published source, runs one complete
   verification, and records the source snapshot, generated inputs, and
   official answers as a Native Package materialization.
3. If the stored payload is unavailable or fails checksum checking, the
   service invalidates it and its cached external packages, then repeats the
   full Verification in that same export job.
4. A Native request finishes when the Native Package is ready. A DOMjudge,
   ICPC 2025-09, QOJ, or Nowcoder request then runs the corresponding adapter or
   reuses the requested cached external package.

The Native Package is downloaded directly. A request that must first prepare it
has a Package Export attempt, but it creates no `exports` row or second archive.
Downloading an existing Native Package creates no job. An adapter consumes only
a checksum-checked `NativePackageReader` and caller-owned staging; it
cannot read Git, workspaces, Verification tables, runtime cache, or another
adapter's output, and cannot start Verification.

Publishing a newer official version leaves older Native Packages tied to their
original versions. Package Export always targets the published version
that was current when the request was accepted; history downloads are read-only.
Only one Package Export for a problem/version can run at a time, and a competing
request fails immediately rather than waiting.

## Contest package download lifecycle

Contest definitions, membership, problem indices, statement folders, and
contest attachments are durable authoring state. A problem's `idx` is both its
display identity and the sole current-roster order; there is no independent
position. A Contest package download requires every roster problem's current
published revision to have a ready Native Package.

The request rechecks readiness, opens each exact Native Package with its stored
archive checksum, and invokes the selected registered external adapter in
canonical Contest order. It never runs Verification, repairs a Native Package,
or creates a problem-level external-package cache entry. Failure of any child
package aborts the whole response.

The outer bundle and its child archives are request-owned temporary files. The
response blocks until the bundle is complete and deletes the temporary tree
after transfer. It creates no Contest job, build history, or durable Contest
artifact.

## Invalidation and cleanup

| Event | What changes | What remains unchanged |
| --- | --- | --- |
| Edit a workspace | Its verification signature and uncommitted content | Official versions, other workspaces, packages, Contest package downloads |
| Publish | The current official version advances | Earlier versions and derived products already tied to them |
| Remove access | The next authorization query denies the removed capability | Source and derived bytes are not deleted as a side effect |
| Restart the application | Active jobs fail; runtime queues, leases, and cache payloads are cleared | Git history, workspaces, users, contests, and durable contest source |
| Detect a missing or corrupt Native Package payload | It becomes unavailable and its cached external packages are invalidated; a Package Export may rebuild it | Its official source version remains available |
| Run generated-data cleanup | Verification, package, export, and preview rows and files are removed | Git history, workspaces, users, problem metadata, contest definitions, Contest source, and operator backups |

Cleanup removes Native Package materializations and cached external packages
for an official problem version. The version remains in source history and can
be verified and packaged again.

## Design consequences

- Mutable authoring and fixed delivery inputs are separate states.
- Every asynchronous verification or build records the source identity it
  consumes before doing expensive work.
- Verification status and verification-cache availability are separate facts.
- A live derived record and its filesystem payload form one object; missing or
  mismatched bytes are an integrity failure.
- Derived files can be deleted and rebuilt; authored source and durable Contest
  source are outside generated-data cleanup.
- Validation occurs when external input enters the system and when stored
  derived products are reopened. Internal consumers use the accepted canonical shape
  instead of maintaining parallel compatibility representations.

The detailed state transitions and data shapes are defined by the
[problem source](../protocol/problem-source.md),
[execution](../protocol/execution.md), [package](../protocol/package.md), and
[storage](../protocol/storage.md) protocols.
