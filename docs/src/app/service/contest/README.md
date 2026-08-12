# `app/service/contest`

Owns contest identity, membership, properties, problem roster, statement source
and attachments, build jobs, selected build inputs, and derived-output records. It
accepts canonical contest/problem identities and source payloads and returns
access contexts, roster/build snapshots, and validated output download paths.
Focused build services own build admission and terminal transitions, durable
source snapshots, statement assembly and compilation, and Contest-specific
package construction. HTTP adapters only validate request syntax, translate
capability failures, and construct responses.

Relational state lives in the `contest_*` tables. Statement source and
attachments live below the contest source root; products live below the global
`artifacts_root/contests` tree. Build jobs are frozen before asynchronous work
is admitted, and cleanup may remove products without deleting the durable
contest definition. Storage ownership is defined by the
[storage protocol](../../../../protocol/storage.md). Build commands enforce
Contest capability through the access query even when invoked outside HTTP,
and source snapshots resolve paths only through the platform storage layout.

New roster entries are admitted under the active `CONTEST_MAX_PROBLEMS` policy.
The store serializes count, position allocation, and insert in one writer
transaction. Existing contests above a newly lowered limit remain usable; only
further additions are rejected.

Package builds consume canonical problem ICPC exports. The contest service
safely stages each archive, rewrites only the legacy DOMjudge `short-name` to
the frozen contest label, and repacks a contest-owned ZIP. These variants live
under the contest job and never become problem export cache records. This
trusted internal transformation streams payloads and validates archive
structure and member safety without reapplying authenticated-upload budgets.
