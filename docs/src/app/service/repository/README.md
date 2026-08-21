# `app/service/repository`

Owns Git mechanics and repository-backed workspace coordination: repository and checkout provisioning, status, history, diffs, publication, merge comparison, and per-workspace locking.

Committed source and history live in bare Git repositories, checkout contents live in the workspace root, and identity metadata lives in SQLite. Merge previews are process-local. File and archive operations belong to the sibling
[workspace service](../workspace/README.md). The source authority is defined by
the [problem-source protocol](../../../../protocol/problem-source.md).
