# `app/service/problem_package`

Owns the transition from one immutable published Git commit to a verified
revision. Its inputs are the problem identity, published commit and revision
number, and the complete full-verification builder. Its public read boundary is
`VerifiedRevisionReader`: committed source, manifest, and verified testcase
payloads that have already passed archive and source integrity checks.

The service keeps at most one verified-revision identity for each
problem/source commit. Build and physical materialization rows live in SQLite;
archives live below the cleanup-safe `artifacts_root`. Reuse validates archive
size and SHA, safe ZIP shape, manifest identity, all declared payloads, source
digest, and canonical problem source. A corrupt payload becomes unavailable and
invalidates its projections. Rebuilding the same Git revision keeps its
verified-revision identity.

Startup removes staging and fails interrupted builds but does not traverse and
open every completed archive. Integrity is checked at the actual read boundary.
The reader holds extracted files only for its context lifetime and verifies that
the stored archive identity did not change while it was open. A caller with a
frozen checksum can require that exact value.

Package Export is the only orchestrator that may ask this service to prepare a
missing or unavailable verified revision. Contest builds and pure projectors
may only open an already available frozen revision. The Polygon Replica package
download returns the verified revision's own serialization without creating an
export job or projection row.

Published-revision identity, verification provenance, archive availability,
and whether the revision is current are distinct facts. The user-visible
lifecycle and archive contract are specified by the
[package protocol](../../../../protocol/package.md).
