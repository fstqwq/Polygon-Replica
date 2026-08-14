# `app/service/export`

Owns Package Export jobs, derived-package cache lookup and publication, and the
package adapter registry. A request freezes the current published commit. Its worker
prepares or reuses that commit's verified revision, then builds or reuses the
requested `domjudge` or `icpc-2025-09` package. A separate request keeps a
separate job ID even when it resolves to the same cached archive.

Only one Package Export for a problem/commit is admitted at a time. Jobs expose
the phases `queued`, `verifying`, `packaging`, and `complete`; interrupted jobs
become failed at startup. Missing or mismatched derived-package bytes invalidate
only their cache row. Corruption in the underlying verified revision is handled
by the problem-package workflow before an adapter runs.

`adapters/` contains one module per external package format and the single
authoritative registry. Callers enumerate `PackageAdapterRegistry.adapters` or
`formats` and resolve a format with `require()`; they do not maintain format
string lists of their own. Each adapter owns the policy and output layout of
exactly one external format; shared filesystem mechanics live in the same package.
They receive an already validated `VerifiedRevisionReader`, canonical naming
options, and a caller-owned empty staging directory. They may render
statements, but do not access SQLite, Git, workspaces, verification rows,
runtime cache, or another adapter output. They do not create jobs or publish
archives. Both single-problem Export and Contest builds invoke this boundary
and own their respective atomic publication.

The Polygon Replica package is not an export format: its direct download is
owned by verified-revision history. Format layouts, cache identity, and failure
behavior are defined by the
[package protocol](../../../../protocol/package.md).
