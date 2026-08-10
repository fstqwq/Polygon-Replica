# `app/service/problem_package`

Owns Native materialization for an immutable published source commit. Its inputs
are the problem id, published Git revision, matching successful verification,
and still-available generated input/answer locators. It returns readiness,
materialization records, validated package readers, or the Native archive path.

Build and materialization rows are durable in SQLite; archives live below the
cleanup-safe artifact root. A build moves through queued/running/terminal
phases, interrupted builds become failed at startup, and a missing or invalid
archive invalidates its materialization. Package readiness, source commit
provenance, and physical availability remain separate, as specified by the
[package protocol](../../../../protocol/package.md).
