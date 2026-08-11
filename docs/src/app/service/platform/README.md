# `app/service/platform`

Contains shared mechanisms: worker queue, admission gate, maintenance,
filesystem/path helpers, Git/process execution, runtime blobs and cache indexes,
locking, crypto, configuration persistence, and workspace path safety. Typed
configuration definitions and the active snapshot live in `app/config/`;
platform configuration services validate and persist registry overrides.

The worker queue is process-local. Its JSONL is diagnostic history and is reset
with in-memory records at startup. Platform mechanisms should not invent domain
status transitions; current coupling in maintenance/configuration is tracked in
the findings ledger.
