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
              +---- full verification ----> verified problem archive (Native)
                                                     |
                                                     +----> ICPC problem package

contest definition and source + selected official problem versions
                              |
                              v
                  statement PDF or ICPC bundle
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
| Verified problem archive | One official problem version and a successful full verification of that exact source | Package build | Derived; reusable while its integrity check succeeds |
| ICPC problem package | One verified problem archive | Export build | Derived; reusable for the same verified archive |
| Contest output | Contest definition and source plus selected official problem versions | Contest build request | Derived |

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

## Package lifecycle

A package request first records the current official problem version. It never
reads a user's changing workspace. For that version:

1. An existing verified problem archive is integrity-checked and reused.
2. If no archive exists, the service extracts the official source, runs the
   complete verification required for packaging, and stores source, generated
   inputs, and official answers in one archive.
3. Native download returns that verified archive. ICPC export converts the same
   archive directly.

The UI calls the verified Polygon Replica archive a **Native package**. It is a
rebuildable delivery input, not another source revision.

A failed integrity check marks the archive unavailable, removes its derived
exports, and requires an explicit rebuild. Publishing a new official version
leaves an older valid archive tied to its original version. A package request
for the new version creates or reuses the archive belonging to that version.

## Contest build lifecycle

Contest definitions, membership, problem order, labels, statement folders, and
contest attachments are durable authoring state. A Contest build derives
delivery products from that state and the official versions of its problems.

Starting a build selects the official version of every problem in the roster.
The build uses those versions and the contest content selected for that request;
later edits belong to a later build. Missing or invalid problem packages fail
the affected build rather than silently substituting different source.

Statement PDF and ICPC bundle are published independently. If one succeeds and
the other fails, the successful output remains available and the job is
`partial`; the system does not publish a bundle missing one of its requested
problems.

## Invalidation and cleanup

| Event | What changes | What remains unchanged |
| --- | --- | --- |
| Edit a workspace | Its verification signature and uncommitted content | Official versions, other workspaces, packages, Contest builds |
| Publish | The current official version advances | Earlier versions and derived products already tied to them |
| Remove access | The next authorization query denies the removed capability | Source and derived bytes are not deleted as a side effect |
| Restart the application | Active jobs fail; runtime queues, leases, and cache payloads are cleared | Git history, workspaces, users, contests, and durable contest source |
| Detect a missing or corrupt verified archive | The archive becomes unavailable and its derived exports are invalidated | Its official source version remains available for rebuild |
| Run generated-data cleanup | Verification, package, export, preview, and Contest-build rows and files are removed | Git history, workspaces, users, problem metadata, contest definitions, Contest source, and operator backups |

Cleanup removes the generated packages for an official problem version. The
version remains in source history and can be packaged again.

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
