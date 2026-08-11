# `app/service/export`

Owns export job records, conversion-cache lookup, Native passthrough, and ICPC
archive construction. It consumes an available, integrity-checked Native
materialization; ICPC conversion also consumes statement rendering/TeX
compilation. It writes export metadata to SQLite and archive bytes below the
artifact root.

The HTTP layer schedules conversion on the process-local worker queue. Startup
marks interrupted job rows failed; completed rows survive, while missing or
checksum-mismatched archive bytes are treated as unavailable and their cache
record is discarded. There is one cache artifact per materialization and export
type, while each request retains its own export job. Current output and cache identity are defined by the
[package protocol](../../../../protocol/package.md).
