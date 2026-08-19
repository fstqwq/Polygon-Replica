# SQLite persistence

The canonical schema is the `SCHEMA` declaration and validation manifest in
`app/db.py`. SQLite does not store committed source files or large derived
payloads. The physical table inventory is in
[the SQLite implementation map](../implementation/sqlite.md).

Foreign keys identify domain relationships. JSON columns carry structured
details whose owning service defines their shape; they are not interchangeable
with filesystem payloads.

## Execution rows

`verifications` is the durable summary for both full verification and custom
run, distinguished by `kind`. It records source/signature context, mode, pass
limit, run configuration, status, failure fields, and timestamps.

`verification_tasks` stores the task DAG and `result_json`. Every downloadable
verification-cache locator is indexed in the currently named
`verification_task_artifacts` table.

Verification admission is insert-only. It creates a `queued` parent without
task rows. Activation uses one `BEGIN IMMEDIATE` transaction to compare-and-set
that parent to `running`, store its complete detail, and batch-insert the entire
validated graph. A zero-row compare-and-set reports an already-running, closed,
or missing verification; it never deletes or rewrites a graph. Each task row's
`program_id` is its durable membership in a verification program. A program
means one source path and compile specification whose test tasks share one
compilation; it is not an arbitrary task group. The accepted solution,
generator, and each checked solution have distinct program identities.

The immutable plan owns program definitions. Task rows denormalize `program_id`,
task kind, source path, and expected behavior. Judgehost validates the
corresponding compile identity when
cases join the program's batch. A task ID is exactly
`vt~<verification_id>~<program_id>~<test_name>`. Constructing the key performs
no database read; activation recomputes every key and rejects a mismatch,
duplicate, or inconsistent program definition as an invalid plan.

Task completion uses one `BEGIN IMMEDIATE` transaction under the verification
runtime write lock. For each task whose `final_status` is empty, that transaction
writes the terminal status, bounded canonical result, finish time, every pass
cache-payload ownership row, generated input or accepted answer ownership, and the
verification's first non-empty failure reason.
Generator content deduplication, skipped-generator results, and pending
descendant skips are included in the transaction. Generator, `main-correct`,
and unexpected task cancellation failures are hard: they also change the
parent to `failed` and cancel remaining open tasks in that transaction. An
explicit user cancellation instead changes the parent to `cancelled`. A solution mismatch is
soft: its first reason is persisted while other solution tasks continue; the
last task transaction changes the parent to `failed` if that reason is present.
A normal last completion otherwise finishes the parent or durably claims
sanity processing. Sanity output and its final parent compare-and-set are
committed together, so concurrent cancellation cannot be overwritten. Any
failure rolls back all writes; process-local indexes and coordinators are
synchronized only after the transaction commits.

An already-terminal task is not rewritten. Completion returns its persisted
result and locators as the effective state, so duplicate or conflicting
callbacks cannot attach a new locator or failure reason.

The Judgehost completion publisher marks an in-memory case acknowledged only
after this transaction succeeds or reports the task already terminal. A failed
transaction leaves the case unacknowledged so the callback receives non-2xx and
can be retried. Custom-run cases have no verification task identity; their
terminal result is retained by the process-local Judgehost scheduler and does
not enter this verification transaction.

`verification_task_diagnostics` stores at most one late-diagnostic snapshot per
task. Its columns are `task_id`, `snapshot_json`, and `updated_at`. A diagnostic
append uses one `BEGIN IMMEDIATE` read/merge/upsert transaction. A content digest
deduplicates retries; unchanged snapshots do not write. The bounded ordered
snapshot retains the newest items when it exceeds the auxiliary display limit.
This table is not a second completion store: appending diagnostics cannot update
`result_json`, terminal status, cache refs, parent status, or `fail_reason`.

Cancellation and failure compare-and-set a `queued` or `running` parent to
`cancelled` and `failed`, respectively. Both preserve the first non-empty reason
and cancel every open task in one transaction. Startup recovery always uses
`failed`, because a process interruption is not a user cancellation, before
runtime blobs and Judgehost state are cleared. Recovery failure aborts
application startup.

Verification detail is read as one SQLite snapshot containing parent, detail,
tasks, refs, and late diagnostics. A process-local runtime overlay is applied
after that read and is not persisted as a competing state authority.

### Linearization guarantees

`BEGIN IMMEDIATE` serializes the aggregate writers. If activation commits before
cancellation, readers see a complete running graph and cancellation then
terminalizes it; if cancellation commits first, activation's `queued` compare-
and-set changes no row and inserts no task. Duplicate workers are the same race:
only one can perform `queued -> running`.

Task completion and cancellation likewise compete on the open task and active
parent predicates. The first writer fixes the terminal result; the second may
only observe it and cancel other open tasks. Sanity completion requires a still-
running parent, so an earlier cancellation cannot be overwritten. A crash or
statement failure during activation exposes either the complete plan or the
original taskless `queued` row, never a partial graph.

Diagnostic appends serialize their read/merge/upsert independently. They can
race with one another without a lost-update window; bounded snapshot eviction
remains the only removal rule. Their table has no write path to any decision
column. This separation makes a late diagnostic incapable of reopening
execution even when it arrives concurrently with completion or cancellation.

Legacy Preview, Package Export, Native Package build, and Contest-job rows
survive normal restarts. Unfinished rows are moved to `failed` because their
process-local work cannot resume. Startup does not open and validate every
completed Native Package archive; integrity is checked when a consumer opens
one. Administrative generated-data cleanup removes the
execution/package/export/build subset described by the
[storage protocol](storage.md#maintenance-cleanup), while identity, authoring,
contest source, attachment, and configuration rows remain.

`problem_package_materializations` owns the durable identity and locator for one
Native Package per problem/source commit. `exports` owns its cached
`domjudge`, `icpc-2025-09`, `qoj`, and `nowcoder` external packages. `export_jobs` owns
request attempts:
distinct requests retain distinct job IDs even when they finish by referencing
the same cached external package. A Native preparation attempt finishes with a
materialization reference and a null `export_id`; it does not add a second
archive. Direct Native Package downloads create no job or `exports`
row. Contest child packages are Contest-owned temporary bundle members and do
not enter `exports` or `export_jobs`.

`statement_previews` stores only user-scoped, disposable Problem/Contest HTML and PDF
Preview request metadata and terminal summaries. The payloads live below the
cache root. Startup/deploy invalidates every row and clears every payload,
including previously successful results; no Pandoc, Poppler, TeX, Lua-filter,
renderer, executable, container, or toolchain version/hash is part of the
Preview input identity. A renderer/toolchain deployment therefore requires the
same global cache invalidation rather than an identity-format change.

Contest roster rows persist `idx`, not a separate position. The application
sorts the complete bounded roster by its shared natural problem-index ordering;
SQLite collation is not an ordering authority. A complete `id -> idx` edit
uses temporary non-canonical values to exchange unique indices and increments
`source_generation` once in the same writer transaction; an unchanged mapping
performs no write. Current Contest package downloads persist no job, build item,
or artifact row. They recheck the current published-package readiness and
archive checksums in the request before producing a temporary response file.
The retained Contest build tables describe historical data only and are
removed by normal generated-data cleanup.

Contest identity and lifecycle remain on `contests`. Editable Contest metadata
is a sparse string mapping in `contest_properties`, keyed by
`(contest_id, key)`. Property names use lower-case letters, digits, and
underscores after a lower-case initial, and may also use upper-case letters for
lower-camel-case FTL names. A single optional dot is reserved for a language override:
`<property>.<language>`, for example `title.chinese`. The suffix is normalized
with the Statement language token rules; an absent override inherits the base
value. Every effective base key is injected directly into the Contest
`statements.ftl` context and is also available through the `properties`
mapping. The renderer reserves its structural context keys, including
`contest`, `language`, and `statements`; they cannot be property names. Empty
optional values are represented by absent rows rather than empty strings.
The Properties UI describes a base value as applying to `All` languages; a
`.<language>` row is the narrower override.

`title` is required and cannot be deleted. The default template recognizes the
optional `insertBlankPage` and `banner` properties: `insertBlankPage` accepts
`true` or `false` and resolves to Boolean false when absent, while `banner`
defaults to an empty string and may use a language override. Removing either
property's saved values restores that default. Other property groups may be removed
together with all of their language overrides. A single mapping edit increments
the owning Contest's `source_generation` once when at least one value changes
and performs no write when the mapping is unchanged.

The Properties page can seed two ordinary mapping values. `Insert Default
Banner` creates a `banner` value that renders `\contestname` in the existing
statement header slot. `Insert Blank Page After Odd Statements` sets
`insertBlankPage` to `true`. After insertion they are edited and removed like
the corresponding mapping entries; the shortcuts do not introduce a separate
property type.

## Agent authorization rows

`agent_sessions` owns the connected desktop identity and its non-expiring
`none`, `readonly`, `workspace`, or `commit` general scope. Ordinary Agent API
authentication uses a random `polygon_agent_...` bearer credential. SQLite
stores only its SHA-256 verifier; the raw credential exists only in the Agent
state file and is returned only when a registration URL creates or reconnects
a session. The identity hash describes stable client metadata and is used to
validate an explicit reconnect, never as an authentication secret. A
disconnected session is not an actor. The identity-header release boundary
does not preserve old sessions or grants; those clients register new sessions.

`agent_problem_grants` stores independent user approvals. Multiple rows may
target the same session and problem; each retains its own scope, creation time,
expiry, and revocation time. Authorization filters active rows at read time and
takes their strongest scope without merging their lifetimes. The result is
combined with general scope and capped by the user's current direct,
Contest-derived, or administrator problem role.

`agent_access_requests` is an expiring approval request, not credential
delivery. Approval creates one grant and records its ID, scope, and expiry in
the same `BEGIN IMMEDIATE` transaction. Repeating approval of the same request
returns that grant and cannot create another row.

## Configuration

`system_config` is a key/value JSON store with update identity and timestamp. It
is the application authority for settings such as secure-cookie behavior; there
is no environment-variable override for that setting.

## Schema changes

An absent or empty database is initialized directly with the canonical schema.
An existing database is opened read-only during startup validation. Every
canonical table, required column, and named index must already exist. Missing
objects put the process in a schema-blocked state: worker and runtime startup do
not run, and every HTTP request receives a raw `503` that lists the missing
objects and directs the administrator to upgrade the database offline.

Startup never applies DDL to an existing database. Extra tables, columns,
indexes, and rows are accepted and preserved; they do not satisfy any current
application requirement and are not interpreted as compatibility state.
Existing constraints and definitions behind already named indexes are not
compared against the DDL. A schema change updates the DDL, required-object
manifest, service queries, cleanup policy, offline operator procedure, and this
document together. An upgrade that introduces required schema objects includes
its stopped-service procedure with that release.

The `idx`-only Contest roster schema requires the stopped-service
[Contest problem-index upgrade](../operations/deployment.md#contest-problem-index-upgrade).
The upgrade preserves current roster identities and copies each historical
build's former position unchanged into its frozen `ordinal`; it does not resort
old jobs.

The Contest property-map schema requires the stopped-service
[Contest property-map upgrade](../operations/deployment.md#contest-property-map-upgrade).
