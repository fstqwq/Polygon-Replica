# Storage and cleanup protocol

## Storage authorities

| Store | Current responsibility | Cleanup behavior |
| --- | --- | --- |
| Bare Git root | committed problem sources and history | durable |
| SQLite database | identities, metadata, configuration, summaries, locators | durable; selected rows are maintenance-cleanable |
| Workspace root | mutable per-user Git workspaces | durable until explicitly removed |
| Contest source root | durable contest statement source and attachments outside problem Git | durable |
| Artifact root | exports, `contests/` build products, temporary snapshot archives | cleanup-safe |
| Cache root | previews, verification payloads, JudgeFS, workdirs, queue history, runtime blobs | startup/maintenance-cleared |
| Backup root | operator-managed contest migration archives | permanent and never cleared by application cleanup |

Configured roots MUST resolve to distinct intended locations. Archive members,
user paths, and stored relative locators MUST remain below their owning root and
MUST NOT escape through `..`, absolute paths, or symlink traversal.

## Locators and availability

Database rows store typed relative paths or immutable runtime blob references,
not arbitrary host paths. The owning store validates a locator before resolving
it. A locator is not proof that its payload still exists: cleanup-safe artifacts
are checked when read or downloaded.

Verification input and answer blobs use `verification_artifact_refs`. Other
execution artifacts are referenced inside structured result JSON. JudgeFS
executable blobs and indexes are runtime data. Export and contest artifact rows
refer to files below the global artifact root. `ContestService.artifacts_base()`
resolves contest products specifically below `artifacts_root/contests`; contest
source paths never own derived artifacts.

## Startup cleanup

Before the worker queue starts, the application:

1. reconciles unfinished previews, verifications, contest jobs, exports, and
   Judgehost work;
2. clears the runtime cache index;
3. resets worker history in memory and removes its JSONL;
4. recreates cache artifact and runtime roots.

The cache-root runtime tree, JudgeFS index/blobs, Judgehost workdirs, and worker
history do not survive startup. Durable terminal summary rows can survive even
when cleanup-safe payloads do not.

## Maintenance cleanup

Administrative cleanup closes admission, checks for active work, removes the
configured cleanup-safe metadata and filesystem trees, resets process-local
state, vacuums SQLite, and appends an audit event. It never removes the backup
root. Cleanup is exclusive; worker history reset fails if queued or running jobs
remain.
