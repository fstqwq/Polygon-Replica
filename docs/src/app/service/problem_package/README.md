# `app/service/problem_package`

Owns verified Native materialization from a published source commit. It checks
verification provenance and artifact availability, builds the manifest/test
payload tree, stores the archive, and records materialization identity and
availability.

Package readiness, source commit provenance, and physical archive availability
remain separate fields rather than one aggregate state.
