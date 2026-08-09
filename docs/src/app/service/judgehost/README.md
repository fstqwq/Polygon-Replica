# `app/service/judgehost`

Implements the DOMjudge-compatible worker adapter: host registration and
telemetry, work selection, batch/case leases, file dispatch, JudgeFS cache,
version reports, callbacks, result conversion, and cancellation.

Hostname is the lease identity and is validated at the boundary. Authenticated
reports are trusted execution facts. Compiler/runner versions are telemetry,
not current admission or cache gates. Result processing is large and
multi-responsibility; PLC-006/007 track that refactor.
