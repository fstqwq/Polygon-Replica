# `app/service/export`

Owns Package Export jobs, projection-cache lookup and publication, and the two
package projectors. A request freezes the current published commit. Its worker
prepares or reuses that commit's verified revision, then builds or reuses the
requested `domjudge` or `icpc-2025-09` projection. A separate request keeps a
separate job ID even when it resolves to the same cached archive.

Only one Package Export for a problem/commit is admitted at a time. Jobs expose
the phases `queued`, `verifying`, `projecting`, and `complete`; interrupted jobs
become failed at startup. Missing or mismatched projection bytes invalidate
only their cache row. Corruption in the underlying verified revision is handled
by the problem-package workflow before projection.

`PackageProjectionService` is a pure filesystem boundary. It receives an
already validated `VerifiedRevisionReader`, one external format token,
canonical naming options, and a caller-owned empty staging directory. It may
render statements, but it does not access SQLite, Git, workspaces, verification
rows, runtime cache, or another projection. It does not create jobs or publish
archives. Both single-problem Export and Contest builds invoke this same
boundary and own their respective atomic publication.

The Polygon Replica package is not an export format: its direct download is
owned by verified-revision history. Format layouts, cache identity, and failure
behavior are defined by the
[package protocol](../../../../protocol/package.md).
