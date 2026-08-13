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
              +---- full verification ----> verified revision
                                                     |
                         +---------------------------+-------------------+
                         |                 |                    |        |
                         v                 v                    v        v
             Polygon Replica package  DOMjudge package  ICPC 2025-09  Contest
                                                                       outputs
```

Generated tests, official answers, PDFs, logs, and packages never flow back
into authored problem source. Import converts an external package into
workspace source, which then follows the same authoring and publication flow as
a problem created in Polygon Replica.

Persisted files belong to one of three classes:

- **Source** is authored state: problem Git history, user workspaces, and
  Contest source and attachments. Source is durable, and its database identity
  and filesystem content must correspond.
- **Derived** data is generated delivery products such as verified problem
  archives, ICPC packages, and Contest outputs. They survive restart. Their
  database record and filesystem payload describe one object; a missing or
  mismatched payload is an integrity failure. Maintenance cleanup removes both.
- **Cache** is disposable execution and preview data. Cache entries may be
  missing, are never a source of truth, and are cleared at application startup.

## State and derivation

| State | Derived from | Fixed at | Lifetime |
| --- | --- | --- | --- |
| Workspace | A new problem, imported source, or an existing official version | Not fixed; its owner continues editing it | Durable until explicitly deleted |
| Official problem version | A reviewed workspace published to the problem | Publish | Durable Git history |
| Preview | One workspace and its statement/sample inputs | Preview request | Cache |
| Workspace verification | One workspace snapshot and selected verification targets | Verification admission and activation | Cleanup-safe database record; program input, output, answer, feedback, transcript, and logs are cache |
| Verified revision | One official problem version and a successful full verification of that exact source | Package export verification phase | Derived; reusable while its source snapshot and verified test data remain intact |
| Polygon Replica package | One verified revision | Direct download | The verified revision's own downloadable serialization; no export job |
| DOMjudge package | One verified revision; standalone exports use its canonical problem slug | Package projection | Derived; reusable for the same verified revision and format |
| ICPC Problem Package 2025-09 | One verified revision | Package projection | Derived; reusable for the same verified revision |
| Contest output | Contest definition/source where needed plus a frozen mapping of roster entries to verified revisions | Contest build request | Derived |

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

A process restart cannot resume the in-memory scheduler, Judgehost leases, or
worker queue. Startup marks interrupted work failed and clears verification
cache. Completed result rows may remain for display, while their downloadable
program input, output, answer, feedback, transcript, and logs become
unavailable.

## Verified revision and package lifecycle

A Package Export request freezes the current official problem version. It never
reads a user's changing workspace. For that version:

1. An existing verified revision is fully integrity-checked and reused.
2. If none exists, the service extracts the published source, runs one complete
   verification, and records the source snapshot, generated inputs, and
   official answers as the verified revision.
3. If the stored payload is unavailable or fails integrity checking, the
   service invalidates it and its projections, then repeats the full
   verification in that same export job.
4. Once the verified revision is ready, the requested DOMjudge or ICPC 2025-09
   projection is built or reused.

The Polygon Replica package is a direct download of the verified revision. It
does not create an export job. A projection consumes only an integrity-checked
verified-revision reader and caller-owned staging; it cannot read Git,
workspaces, verification tables, or runtime cache, and cannot start
verification.

Publishing a newer official version leaves older verified revisions tied to
their original versions. Package Export always targets the published version
that was current when the request was accepted; history downloads are read-only.
Only one Package Export for a problem/version can run at a time, and a competing
request fails immediately rather than waiting.

## Contest build lifecycle

Contest definitions, membership, problem order, labels, statement folders, and
contest attachments are durable authoring state. A Contest build derives
delivery products from that state and verified problem revisions that already
exist.

Build admission freezes the ordered roster and, for each problem, selects its
highest available verified revision. That revision may trail the current
published version. The readiness state is `current`, `stale`, or `none`; `none`
rejects the build without creating a job. Contest admission and workers never
run Verification, repair a verified revision, or create a problem-level export.

The build items store each selected verified revision's identity and archive
checksum. Workers check both before and after reading it. A changed or corrupt
payload fails the requested outputs instead of falling back to another
revision.

Statement PDF, DOMjudge bundle, and ICPC 2025-09 bundle are independent output
choices. Statement compilation reads statement source, samples, and assets
directly from the verified revision. Package bundles invoke the same pure
projectors as single-problem export, with the frozen Contest label used as the
DOMjudge short name. Child archives are temporary Contest-owned members and do
not enter the problem export cache. Each bundle is all-or-nothing; successful
outputs remain available when another requested output fails and the job becomes
`partial`.

## Invalidation and cleanup

| Event | What changes | What remains unchanged |
| --- | --- | --- |
| Edit a workspace | Its verification signature and uncommitted content | Official versions, other workspaces, packages, Contest builds |
| Publish | The current official version advances | Earlier versions and derived products already tied to them |
| Remove access | The next authorization query denies the removed capability | Source and derived bytes are not deleted as a side effect |
| Restart the application | Active jobs fail; runtime queues, leases, and cache payloads are cleared | Git history, workspaces, users, contests, and durable contest source |
| Detect a missing or corrupt verified revision payload | It becomes unavailable and its projections are invalidated; a Package Export may rebuild it | Its official source version remains available |
| Run generated-data cleanup | Verification, package, export, preview, and Contest-build rows and files are removed | Git history, workspaces, users, problem metadata, contest definitions, Contest source, and operator backups |

Cleanup removes verified revisions and projections for an official problem
version. The version remains in source history and can be verified and packaged
again.

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
