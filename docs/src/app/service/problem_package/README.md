# `app/service/problem_package`

Owns the transition from one immutable published Git commit to a Native
Package. Its inputs are the problem identity, published commit and revision
number, and the complete full-verification builder. Its public read boundary is
`NativePackageReader`: committed source, manifest, verified testcase
payloads, and the derived offline statement tree carried by the archive.

The service keeps at most one Native Package materialization identity for each
problem/source commit. Build and physical materialization rows live in SQLite;
archives live below the cleanup-safe `artifacts_root`. Reuse validates archive
size and SHA, safe ZIP shape, manifest identity, all declared payloads, source
digest, and canonical problem source. A corrupt payload becomes unavailable and
invalidates its cached external packages. Rebuilding the same Git revision
keeps its materialization identity.

The archive preserves authored `statement/` paths and renders each language to
`statement-build/<language>/`. That build tree is reproducible, is excluded
from the Git-source digest, and is not added to the Native Package manifest. In
contrast, `test-data/` is consumed by adapters and therefore retains strict
manifest and payload-integrity validation.

Startup removes staging and fails interrupted builds but does not traverse and
open every completed archive. Integrity is checked at the actual read boundary.
The reader holds extracted files only for its context lifetime and verifies that
the stored archive identity did not change while it was open. A caller with a
frozen checksum can require that exact value.

Package Export is the only orchestrator that may ask this service to prepare a
missing or unavailable Native Package. Contest package downloads and adapters
may only open an already available package through `NativePackageReader`. The
Native Package download returns that package's own serialization without
creating an export job or external-package cache row.

Published-revision identity, Verification provenance, Native Package
availability, and whether the package is current are distinct facts. The
user-visible lifecycle and archive contract are specified by the
[package protocol](../../../../protocol/package.md).
