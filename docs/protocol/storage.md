# Storage and cleanup protocol

## Storage classes

Every application-managed file belongs to one of three classes:

| Class | Meaning | Consistency and lifetime |
| --- | --- | --- |
| Source | Authored problem and Contest content | Durable. Its database identity and filesystem content correspond. |
| Derived | A generated delivery product | Survives restart. Its database record and payload correspond until maintenance removes both. |
| Cache | Re-creatable preview, verification, and runtime data | May be absent and is cleared at startup. Durable state must not depend on its presence. |

A missing source file is data loss. A missing or mismatched derived payload is an
integrity failure. A missing cache entry is a normal cache miss or an
unavailable diagnostic payload.

## Storage authorities

| Store | Current responsibility | Cleanup behavior |
| --- | --- | --- |
| Bare Git root | committed problem sources and history | durable source |
| SQLite database | identities, metadata, configuration, summaries, locators | durable; selected rows are maintenance-cleanable |
| Workspace root | mutable per-user Git workspaces | durable source until explicitly removed |
| Contest source root | contest statement source and attachments outside problem Git | durable source |
| `artifacts_root` | verified-revision archives, package projections, and `contests/` build products | derived data; survives startup and is maintenance-cleanable |
| Cache root | preview/verification payloads, temporary snapshots, runtime blobs, JudgeFS data, workdirs, queue history, and import drafts | disposable cache; startup-cleared and maintenance-cleanable |
| Backup root | the single application source backup and operator-managed contest migration archives | permanent and never cleared by application cleanup |

The six managed directory roots MUST be non-root directories, MUST NOT be
symlinks, and MUST NOT contain or overlap one another after resolution. The
database path MUST be a regular-file location outside all managed roots. Archive
members, user paths, and stored relative locators MUST remain below their owning
root and MUST NOT escape through `..`, absolute paths, or symlink traversal.

`app.runtime.ApplicationRuntime` constructs one `StorageLayout` from these
configured roots. That layout is the sole owner of application-derived locations for Git
repositories, workspaces, verification and preview payloads, runtime snapshots
and blobs, uploads and import drafts, package projections, verified revisions,
contest build outputs, staging data, worker history, and source backups. Domain services
receive this layout instead of raw settings and do not concatenate configured
roots independently.

There is currently no per-repository disk quota. Upload and package expansion
limits protect individual admission operations; they are not durable workspace
or Git repository quotas.

## Locators and consistency

Locator shape is column-specific. `workspaces.path` stores the selected checkout
path. Contest attachments and export/package archives use relative locators
under their owning roots. Verification input, output, answer, feedback,
transcript, and log cache payloads use `blob://sha256/...` runtime references.
Services validate relative locators and configured paths before resolving them.

Source and derived locators are ownership links. During correct operation, each
live record resolves to its corresponding filesystem payload. Reads still check
paths, sizes, and integrity where the format defines them; failure is reported
as corruption or unavailability rather than accepted as ordinary state.

Cache locators are observations of disposable data. A durable summary may keep
such a locator after startup cleanup, and reads report the payload unavailable.
Verification program input, output, official answer, feedback, transcript, and
logs are all cache even when their owning verification summary row remains.

All verification cache refs are indexed by the currently named
`verification_task_artifacts` table. The canonical structured task result owns
the execution evidence shape, while the ownership index authorizes and locates
cache downloads without scanning that JSON. JudgeFS executable blobs and
indexes are cache. Verified-revision and projection rows carry archive locators
below the physical `artifacts_root`. Contest outputs are stored below
`artifacts_root/contests`; the Contest
source root owns only authored Contest content.

## Startup cleanup

Before the worker queue starts, the application:

1. fails interrupted verified-revision builds and Package Export jobs, without
   opening every completed archive;
2. in one durable recovery transaction, fails unfinished verifications and
   cancels all of their open tasks; startup stops if that transaction fails;
3. cancels unfinished preview, contest-job, and Judgehost runtime work;
4. resets worker history in memory and removes its JSONL;
5. clears the process-local runtime cache index;
6. deletes every child of `cache_root` and recreates the empty root.

Preview/verification cache payloads, runtime snapshots/blobs, JudgeFS data,
Judgehost workdirs, worker history, uploads, and import drafts do not survive
startup. Durable terminal summary rows can survive after their cache payloads
are cleared. Cache deletion failure aborts startup before workers begin.

## Maintenance cleanup

Administrative cleanup closes both ordinary work admission and the Judgehost
callback admission gate. It refuses to start while requests, callbacks, worker
jobs, or queued/leased/reporting Judgehost work is active. It recreates
the preview, verification, verified-revision, projection, export-job, and
contest-build metadata tables;
empties the entire `artifacts_root` and `cache_root`; resets process-local execution
state; and vacuums SQLite. Recreating those explicitly registered cleanup-safe
tables removes every row and any extra local columns in that domain. Unknown
tables and durable problem, user, workspace, contest, membership, contest
attachment, configuration, and backup data are not guessed at or removed.
The database stage also drops the explicitly registered redundant indexes that
are already covered by current `UNIQUE` or composite primary-key constraints.
It does not infer or remove other operator-created indexes.
Current-process status is held by the in-memory maintenance snapshot; after a
restart, the recovery operation is a safe rerun.

A successful cleanup removes derived records and their payloads as one logical
operation. Database deletion precedes filesystem deletion so an interrupted
cleanup can leave orphan files but cannot leave a live derived record pointing
at a deliberately deleted payload. The failed cleanup remains retryable.

## Source backup

The system-admin source-backup action uses the same exclusive maintenance gate
as generated-data cleanup. It starts only after ordinary requests, Judgehost
callbacks, worker jobs, and queued, leased, or reporting Judgehost work have
drained. The gate stays closed while the archive is built and published.

The archive contains exactly three top-level members:

- `manifest.json`, with bounded creation and source-tree summary data;
- `bare/`, containing every repository and its Git history from the bare root;
- `workspaces/`, containing every workspace, including Git metadata and
  uncommitted files.

SQLite, Contest source, derived data, cache data, existing backup-root content,
application code, and deployment secrets are not included. This is a source
recovery archive, not a complete application-state backup.

The application publishes the pair
`backup_root/source-backup/latest.tar.gz` and `latest.tar.gz.sha256`. It builds
hidden temporary files, reopens and validates the archive and its sidecar, then
atomically replaces the published pair. A handled failure restores the
preceding pair. Only a system administrator can start or download the backup.
Generated-data cleanup never removes it.
