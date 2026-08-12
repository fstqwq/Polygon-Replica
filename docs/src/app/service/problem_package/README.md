# `app/service/problem_package`

Owns Native materialization for an immutable published source commit. Its inputs
are the problem id, published Git revision, matching successful verification,
and still-available generated input/answer locators. It returns readiness,
materialization records, validated package readers, or the Native archive path.

Build and materialization rows are durable in SQLite; archives live below the
cleanup-safe `artifacts_root`. A build moves through queued/running/terminal
phases, interrupted builds become failed at startup, and a missing or invalid
archive invalidates its materialization. Package readiness, source commit
provenance, and physical availability remain separate, as specified by the
[package protocol](../../../../protocol/package.md).

Materializations and export-job history are problem-level read models. Every
user with read access to the problem sees the same package history, regardless
of which user initiated an export or whether a Native materialization was
created by a contest build. Creating a new export still requires problem write
access.
