# `app/service/export`

Owns asynchronous export jobs and persisted export products. It consumes an
available problem-package materialization and writes archives under the artifact
root.

Current outputs include Native products and the single hybrid ICPC ZIP described
by the package protocol. Cached product identity does not imply deterministic
ZIP bytes across rebuilds.
