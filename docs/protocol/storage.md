# Storage and cleanup protocol

## Storage classes

Every application-managed file belongs to one of three classes:

| Class | Meaning | Consistency and lifetime |
| --- | --- | --- |
| Source | Authored problem and contest content | Durable. Its database identity and filesystem content correspond. |
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
| `artifacts_root` | native package archives and cached external packages | derived data; survives startup and is maintenance-cleanable |
| Cache root | HTML/PDF statement preview payloads, transient contest package downloads, verification payloads, temporary snapshots, runtime blobs, JudgeFS data, workdirs, queue history, and import drafts | disposable cache; startup-cleared and maintenance-cleanable |
| Backup root | the single application source backup and operator-managed contest migration archives | permanent and never cleared by application cleanup |

The six managed directory roots MUST be non-root directories, MUST NOT be
symlinks, and MUST NOT contain or overlap one another after resolution. The
database path MUST be a regular-file location outside all managed roots. Archive
members, user paths, and stored relative locators MUST remain below their owning
root and MUST NOT escape through `..`, absolute paths, or symlink traversal.

All filesystem locations are resolved through one validated storage layout. There is no per-repository disk quota; upload and expansion limits apply only to individual admission operations.

## Locators and consistency

| Locator | Meaning |
| --- | --- |
| Workspace path | Checkout below the workspace root. |
| Contest source or package locator | Relative path below its owning durable or derived root. |
| `blob://sha256/...` | Disposable verification or runtime payload below the cache root. |

Source and derived locators must resolve to their recorded payload. Reads validate paths and any format-defined size or integrity evidence. Cache locators may outlive their payload; a missing cache payload is reported as unavailable without changing the durable summary.

Statement preview files, verification evidence, JudgeFS executables, contest bundle staging, and import drafts are cache. Native and external package archives are derived data below `artifacts_root`. Contest source remains authored content below the contest source root.

## Startup cleanup

Before workers start, recovery:

1. fails interrupted verification and package work;
2. invalidates statement previews and judgehost runtime work;
3. clears process-local queues, indexes, and worker history;
4. recreates an empty cache root.

Durable terminal summaries may remain after their cache payloads are removed. Failure to commit recovery or clear cache aborts startup.

## Maintenance cleanup

Administrative cleanup requires drained ordinary work and judgehost callbacks. It removes verification, preview, package, and export metadata; empties `artifacts_root` and `cache_root`; resets process-local execution state; and vacuums SQLite. Authored source, identities, access control, configuration, backups, and unknown operator-owned schema objects remain.

Database deletion precedes filesystem deletion. An interrupted cleanup may leave orphan files but never a live derived record whose payload was deliberately removed; rerunning cleanup is safe.

## Source backup

Source backup uses the exclusive maintenance gate and starts after work and callbacks drain.

| Member | Contents |
| --- | --- |
| `manifest.json` | Creation and source-tree summary. |
| `database/metadata.db` | Transactionally consistent SQLite snapshot, including committed WAL transactions. |
| `bare/` | Problem repositories and Git history. |
| `workspaces/` | Workspaces, Git metadata, and uncommitted files. |
| `contest-sources/` | Authored contest statement source and resources. |

Derived data, cache, existing backups, application code, deployment configuration, and secrets outside SQLite are excluded. The archive contains user, session, access-control, and encrypted configuration data and must be protected like the live database.

The application atomically publishes `backup_root/source-backup/latest.tar.gz` and its `.sha256` sidecar. Only a system administrator can create or download it, and generated-data cleanup never removes it. Restore the database and all three source roots as one point-in-time recovery set.
