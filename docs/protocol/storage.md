# Storage and cleanup protocol

## Storage authorities

| Store | Current responsibility | Cleanup behavior |
| --- | --- | --- |
| Bare Git root | committed problem sources and history | durable |
| SQLite database | identities, metadata, configuration, summaries, locators | durable; selected rows are maintenance-cleanable |
| Workspace root | mutable per-user Git workspaces | durable until explicitly removed |
| Contest source root | durable contest statement source and attachments outside problem Git | durable |
| Artifact root | exports, `contests/` build products, temporary snapshot archives | survives startup; maintenance-cleanable |
| Cache root | preview/verification payloads, runtime snapshots/blobs, JudgeFS data, workdirs, queue history, and import drafts | `artifacts/` and `runtime/` are startup-cleared; the whole root is maintenance-cleanable |
| Backup root | the single application source backup and operator-managed contest migration archives | permanent and never cleared by application cleanup |

The six managed directory roots MUST be non-root directories, MUST NOT be
symlinks, and MUST NOT contain or overlap one another after resolution. The
database path MUST be a regular-file location outside all managed roots. Archive
members, user paths, and stored relative locators MUST remain below their owning
root and MUST NOT escape through `..`, absolute paths, or symlink traversal.

The process composition root constructs one `StorageLayout` from these configured
roots. That layout is the sole owner of application-derived locations for Git
repositories, workspaces, verification and preview payloads, runtime snapshots
and blobs, uploads and import drafts, exports, materializations, contest build
artifacts, staging data, worker history, and source backups. Domain services
receive this layout instead of raw settings and do not concatenate configured
roots independently.

There is currently no per-repository disk quota. Upload and package expansion
limits protect individual admission operations; they are not durable workspace
or Git repository quotas.

## Locators and availability

Locator shape is column-specific. `workspaces.path` stores the selected checkout
path. Contest attachments and export/package archives use relative locators
under their owning roots. Verification input/answer payloads and structured
execution results use immutable `blob://sha256/...` runtime references. Services
validate relative locators and configured paths before resolving them.

A database locator is not proof that its payload is still available. Artifact-
root files can disappear after maintenance cleanup; cache-root payloads can also
disappear at startup. Reads and downloads check the referenced file or blob.

All verification execution refs are indexed by `verification_task_artifacts`.
The canonical structured task result still owns the execution evidence shape,
while the ownership index authorizes and locates downloads without scanning that
JSON. JudgeFS executable blobs and indexes are runtime data. Export and package
rows carry artifact-root archive locators. Contest artifact paths are derived
below `artifacts_root/contests`; the contest source root never owns derived
artifacts.

## Startup cleanup

Before the worker queue starts, the application:

1. fails interrupted package builds and export jobs;
2. in one durable recovery transaction, fails unfinished verifications and
   cancels all of their open tasks; startup stops if that transaction fails;
3. cancels unfinished preview, contest-job, and Judgehost runtime work;
4. resets worker history in memory and removes its JSONL;
5. clears the process-local runtime cache index;
6. deletes and recreates `cache_root/artifacts` and `cache_root/runtime`.

Preview/verification cache payloads, runtime snapshots/blobs, JudgeFS data,
Judgehost workdirs, and worker history do not survive startup. Other cache-root
children are not part of this general startup deletion. Durable terminal summary
rows can survive even when their cleanup-safe payloads do not.

## Maintenance cleanup

Administrative cleanup closes both ordinary work admission and the Judgehost
callback admission gate. It refuses to start while requests, callbacks, worker
jobs, or queued/leased/reporting Judgehost work is active. It recreates
the preview, verification, package, export, and contest-build metadata tables;
empties the entire artifact and cache roots; resets process-local execution
state; and vacuums SQLite. Recreating those explicitly registered cleanup-safe
tables removes every row and any extra local columns in that domain. Unknown
tables and durable problem, user, workspace, contest, membership, contest
attachment, configuration, and backup data are not guessed at or removed.
Current-process status is held by the in-memory maintenance snapshot; after a
restart, the recovery operation is a safe rerun.

## Source backup

The system-admin source-backup action uses the same exclusive maintenance gate
as artifact cleanup. It starts only after ordinary requests, Judgehost
callbacks, worker jobs, and queued, leased, or reporting Judgehost work have
drained. The gate stays closed while the archive is built and published.

The archive contains exactly three top-level members:

- `manifest.json`, with bounded creation and source-tree summary data;
- `bare/`, containing every repository and its Git history from the bare root;
- `workspaces/`, containing every workspace, including Git metadata and
  uncommitted files.

SQLite, contest source, artifacts, cache data, existing backup-root content,
application code, and deployment secrets are not included. This is a source
recovery archive, not a complete application-state backup.

The application publishes the pair
`backup_root/source-backup/latest.tar.gz` and `latest.tar.gz.sha256`. It builds
hidden temporary files, reopens and validates the archive and its sidecar, then
atomically replaces the published pair. A handled failure restores the
preceding pair. Only a system administrator can start or download the backup.
Artifact cleanup never removes it.
