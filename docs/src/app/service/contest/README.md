# `app/service/contest`

Owns contest identity, membership, properties, problem roster, statement source
and attachments, build jobs, frozen build items, and artifact records. It
accepts canonical contest/problem identities and source payloads and returns
access contexts, roster/build snapshots, and validated artifact download paths.

Relational state lives in the `contest_*` tables. Statement source and
attachments live below the contest source root; products live below the global
`artifacts_root/contests` tree. Build jobs are frozen before asynchronous work
is admitted, and cleanup may remove products without deleting the durable
contest definition. Storage ownership is defined by the
[storage protocol](../../../../protocol/storage.md). Some build policy remains
in `app/impl/contest`, as recorded by
[PLC-008](../../../../implementation/findings.md#placement-and-maintainability).

New roster entries are admitted under the active `CONTEST_MAX_PROBLEMS` policy.
The store serializes count, position allocation, and insert in one writer
transaction. Existing contests above a newly lowered limit remain usable; only
further additions are rejected.

Package builds consume canonical problem ICPC exports. The contest service
safely stages each archive, rewrites only the legacy DOMjudge `short-name` to
the frozen contest label, and repacks a contest-owned ZIP. These variants live
under the contest job and never become problem export cache records.
