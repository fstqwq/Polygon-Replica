# `app/service/problem_package`

Owns the transition from one immutable published Git commit to a Native
Package. Its inputs are the problem identity, published commit and revision
number, and a Verification builder that supplies input and main-correct
evidence. Its public read boundary is `NativePackageReader`: committed source,
manifest, materialized testcase payloads, and the derived offline statement
tree carried by the archive.

The service keeps at most one Native Package materialization identity for each
problem/source commit. Build and physical materialization rows live in SQLite;
archives live below the cleanup-safe `artifacts_root`. Construction validates
the canonical problem source, manifest, and declared payloads before writing
the archive. Reuse checks the recorded archive SHA-256; a checksum mismatch
makes the Package unavailable and invalidates its cached external packages.
Rebuilding the same Git revision keeps its materialization identity.

The archive preserves authored `statement/` paths and renders each language to
`statement-build/<language>/`. That build tree is reproducible, is excluded
from the Git-source digest, and is not added to the Native Package manifest. In
contrast, `test-data/` is consumed by adapters and is validated before the
Native Package is serialized.

Startup removes staging and fails interrupted builds but does not traverse and
open every completed archive. The reader checks the recorded SHA-256, safely
extracts the archive for its context lifetime, and parses the manifest. It does
not repeat construction-time payload and source validation or recheck the
archive when the context closes. A caller with a frozen checksum can require
that exact value.

Package Export is the only orchestrator that may ask this service to prepare a
missing or unavailable Native Package. Contest package downloads and adapters
may only open an already available package through `NativePackageReader`. The
Native Package download returns that package's own serialization without
creating an export job or external-package cache row.

Published-revision identity, current full-Verification certification, Native
Package availability, and whether the package is current are distinct facts.
Certification is an SQLite reference on the materialization and is not part of
the archive. The user-visible lifecycle and archive contract are specified by the
[package protocol](../../../../protocol/package.md).
