# Three Refactoring Batches

This plan records three implementation batches in dependency order: safety
boundaries, verification runtime ownership, then Judgehost result-pipeline
convergence. The owning current-state documentation changes with the
implementation.

Tests do not run on Windows. CI runs each of the four resource groups directly
from an Ubuntu checkout and virtualenv through `tests/scripts/test.sh`; it does
not run those groups through `docker-e2e.sh`. For this delivery, a separate
acceptance audit ran the current checkout in an isolated Linux container with
`/opt/polygon-replica/.venv` using:

```bash
for group in unit service executor e2e; do
  /opt/polygon-replica/.venv/bin/python \
    tests/scripts/run_test_groups.py "$group"
done
```

That audit container was not the production application image. The executor
group received a temporary, test-only Ubuntu compiler sysroot because the
application image does not ship a C++ toolchain. Separately,
`tests/scripts/docker-e2e.sh` used the production image and an isolated local
Docker mock to exercise the declared Judgehost protocol. No real Judgehost was
used.

## Delivery record

The safety changes retain their independent review boundaries:

- `5f94c61 Migrate configuration and stream ZIP imports`
- `5685098 Preserve secure host environment configuration`
- `616f5f4 Fail Docker builds on TeX setup errors`

The runtime-registry, execution-service, finalization, normalization, and
publication changes all cross the same runtime composition root. They were
delivered without temporary compatibility wrappers in the single atomic commit
`11970ac Own verification runtime and Judgehost result flow`. The responsibility
steps below remain separate for review, but they are not separate historical
commits.

The follow-up delivery adds lifecycle race/rollback evidence, Preview-to-sample
Docker E2E, real TeX sandbox smoke, the explicit TeX Gyre deployment dependency,
and the endpoint-plus-method Judgehost protocol checks described below. These
follow-up changes are not contained in `11970ac`.

Final acceptance on the current tree records:

- `unit`: 239 tests passed;
- `service`: 141 tests passed;
- `executor`: 68 tests passed;
- `e2e`: 506 tests passed;
- pylint: `10.00/10`;
- syntax, pyflakes, vulture, import-policy, cross-package private-import, test
  resource, and `git diff --check` checks passed;
- the production Docker image built, compiled real `pdflatex` and `xelatex`
  PDFs through bubblewrap, and completed the Preview-to-sample and
  full-verification mock Judgehost workflow.

## Batch 1: Input and deployment safety boundaries

This batch resolves three independent, low-coupling risks without changing the
verification lifecycle. Each subsection is a separate commit.

### 1A. Package archive path safety

Delivered commit:

```text
Reject unsafe package archive paths
```

Implementation:

- Add one shared ZIP-entry canonicalizer under `app/service/importing/`.
- Make the Polygon problem, Polygon contest, and ICPC problem importers use the
  same rules:
  - normalize `\` to `/`;
  - allow harmless `.` components and a supported outer wrapper directory;
  - reject absolute paths, drive-qualified paths, NUL bytes, and `..` traversal;
  - ignore directory entries;
  - reject non-directory entries whose canonical path is empty;
  - reject multiple entries that canonicalize to the same path;
  - check for collisions again after removing a supported outer wrapper.
- Keep package-format detection classification-only. The selected importer
  validates every file entry before reading its marker; detection is not a
  security authority.
- Preserve the existing supported single-wrapper package layout.
- Leave the Native importer unchanged; it already rejects normalized duplicate
  paths.

Observable change:

- Polygon problem, Polygon contest, and ICPC archives that were previously accepted after silently
  omitting or replacing unsafe entries are rejected.
- Successful package layout, problem models, and persisted formats do not
  change.

Tests:

- Extend the existing Polygon and ICPC problem-import unit modules and the
  Polygon contest service module (`tests/test_large_package_import.py`); do not
  create a new resource group.
- Cover `../statement.pdf`, absolute paths, drive-qualified paths, `a/../b`,
  `a\b` versus `a/b`, and a valid wrapped package. The canonicalizer rejects a
  NUL when one is present in its input, but Python's ZIP reader truncates a NUL
  filename before exposing `ZipInfo.filename`, so package-level tests and
  protocol claims do not promise detection of discarded bytes. Wrapper removal
  retains a defensive collision check, although prefix removal is injective
  after the first canonical-map collision check.
- Assert the error category and observable import result, not complete error
  wording or private helper calls.

Documentation:

- Update the package/import protocol with the accepted archive-name boundary.
- Remove `PKG-003` from the findings ledger, which contains current debt rather
  than a history of resolved findings.

### 1B. Installer environment-file safety

Delivered commit:

```text
Preserve secure host environment configuration
```

Implementation:

- Write `/etc/polygon-replica.env` as `root:root` with mode `0600`.
- On installer reruns:
  - update only installer-owned keys;
  - preserve operator-owned configuration, including the encryption key;
  - parse existing single-line assignments as data and never execute the file
    with `source`;
  - reject duplicate keys, malformed assignments, unbalanced quotes, and
    single-line values that request continuation instead of choosing one
    silently.
- Accept an optional `export` prefix only when reading an existing shell-style
  file, and always emit systemd-compatible `NAME=VALUE` assignments. Preserve
  both `#` and `;` comment lines.
- Render unprivileged content to a temporary file, install it as `root:root`
  mode `0600` into a second temporary file beside the `/etc` target, and replace
  the target atomically.
- Preserve the current runtime-user derivation, root-runtime rejection, and
  systemd unit rendering behavior.

Tests:

- Extend the installer executor contract tests.
- Execute the environment renderer for operator-secret preservation, managed
  value replacement, `export` migration, comment preservation, and invalid or
  duplicate record rejection. Statically verify the root-owned `0600` atomic
  install command, ordinary/sudo-origin identity branches, root-runtime
  rejection, and non-root values in the rendered unit; exercise the resulting
  scripts on Linux during deployment validation.

### 1C. Transparent Docker build failures

Delivered commit:

```text
Fail Docker builds on TeX setup errors
```

Implementation:

- Remove broad `|| true` suppression from TeX and package setup.
- Make required `mktexlsr`, `updmap-sys`, and related setup failures terminate
  the build.
- Install the TeX Gyre fonts used by the canonical statement template
  explicitly in both deployment paths instead of relying on apt recommendations
  omitted by the Docker build.
- Where a step is genuinely optional, use an explicit applicability check and
  ignore only its documented not-applicable state.
- Do not change the image entry point or runtime configuration protocol.

Verification:

- Build the complete image in Linux Docker.
- Compile real `pdflatex` and `xelatex` PDFs using the canonical Latin and CJK
  fonts, require the bubblewrap root switch, and check the resulting PDF magic.
- Run the image startup probe and public Preview workflow.

### Batch 1 acceptance

- Targeted `unit` and `executor` tests pass.
- All four test groups pass in a Linux virtualenv. CI uses its Ubuntu jobs; the
  recorded delivery audit used the isolated container command above.
- The Docker image build and smoke tests pass.
- The resource manifest, import policy, static checks, and `git diff --check`
  pass.
- Correct the stale statement in `tests/RESOURCE_GROUPS.md` that still refers to
  six groups; the current groups are `unit`, `service`, `executor`, and `e2e`.
- Keep 1A, 1B, and 1C as separate commits.

Out of scope:

- Verification or Judgehost behavior.
- New package-format versions, compatibility flags, or cache salts.

## Batch 2: Single ownership of Verification Runtime

This batch has two responsibility steps. At completion there is no module-global
coordinator registry, and every execution and cancellation path uses the same
runtime owner. Both steps were delivered in the combined runtime/result commit
listed in the delivery record.

### 2A. Instance-owned runtime registry

Review boundary:

```text
Own verification runtime registry
```

Introduce dependency-light interfaces equivalent to:

```python
class VerificationRuntimeHandle(Protocol):
    def case_leased(...) -> None: ...
    def completion_committed(...) -> None: ...
    def cancelled(...) -> None: ...


class VerificationRuntimeRegistry:
    def register(
        self,
        verification_id: str,
        handle: VerificationRuntimeHandle,
    ) -> None: ...

    def unregister(
        self,
        verification_id: str,
        handle: VerificationRuntimeHandle,
    ) -> bool: ...
```

Registry invariants:

- Registration is insert-only; a duplicate verification ID fails.
- Unregistration requires both the verification ID and the same handle object.
- A stale worker cannot unregister a newer runtime.
- The registry lock protects only the object map. It selects a handle and
  releases the lock before enqueueing an in-memory event, so its scope never
  includes SQLite, Judgehost, blob storage, network operations, or handle
  callbacks.
- A notification for a missing runtime is a valid no-op because SQLite is the
  durable source of truth.
- If direct completion-event delivery fails for a registered runtime, enqueue a
  durable task-snapshot reconciliation. Idle coordinators repeat reconciliation
  so a persisted predecessor completion cannot strand successors.
- If both delivery paths fail and the durable parent is already terminal, the
  idle coordinator drains Judgehost execution before retiring; the immediate
  caller receives an error that preserves both delivery failures.
- Lease-event delivery retries the current registered owner once. A second
  failure propagates to the Judgehost fetch request so the lease can expire and
  be requested again rather than being silently detached from the coordinator.

Production caller migration:

- Runtime composition creates one registry instance.
- Verification DAG execution uses the injected instance to register and
  unregister.
- The completion service receives the registry notification method as its
  post-commit notifier.
- Judgehost dispatch reports leases through an injected consumer-owned
  `CaseLeaseSink`; it no longer imports the verification scheduler.
- Cancellation uses the injected registry instance.
- Delete the module-global coordinator map and all module-level register,
  unregister, and notification functions after every caller is migrated.

The commit must close the activation/cancellation registration race with this
sequence:

```text
durable activation
  -> construct coordinator from the immutable durable graph
  -> register runtime
  -> reload the durable parent snapshot
  -> inject closed state and reload terminal tasks when already closed
  -> otherwise begin coordinator execution
```

This provides a linearization point for all relevant orderings:

- Cancellation before registration is observed by the post-registration
  durable-state read.
- Cancellation between registration and the read finds the registry and queues
  an event.
- Cancellation after the read reaches the already registered runtime.

### 2B. Verification execution service

Review boundary:

```text
Own verification execution lifecycle
```

Add `app/service/verification/execution.py` with a
`VerificationExecutionService` that explicitly receives:

- the verification lifecycle service;
- the verification task store;
- the verification completion service;
- the runtime registry;
- a Judgehost drain port.

It constructs the one canonical coordinator directly; the lifecycle service is
also its durable parent-snapshot reader. A separate factory/reader abstraction
would add a second composition policy without changing the current boundary.

The execution service owns:

- coordinator construction;
- registration, durable-state synchronization, execution, and exact
  unregistration;
- scheduler-exception failure handling;
- user cancellation;
- Judgehost drain ordering.

Every failure and cancellation follows:

```text
SQLite parent/task transition
  -> coordinator event
  -> Judgehost drain
```

The drain is idempotent and must still be attempted if the runtime event enqueue
fails; a failed cancel event falls back to a closed event, and the coordinator
also checks durable parent state after an idle wait. Drain receives one bounded
immediate retry. A later cancellation/failure request also retries drain even
when its lifecycle compare-and-set returns `closed`, so a prior failed drain is
recoverable without reopening SQLite state. A synchronous terminal hard failure
stops the current publication slice before another independent ready root is
exposed. Registration conflicts do not fail the durable verification, and
event/drain/unregistration errors do not mask one another or an earlier
scheduler error.

Workspace continues to own:

- Git snapshots;
- source and test planning;
- payload construction;
- verification-program planning;
- sanity callbacks;
- audit context.

Workspace no longer constructs coordinators, accesses the registry, decides the
cancellation order, directly drains Judgehost, or writes a second verification
failure from an outer exception handler.

Caller migration:

- Worker/context jobs use the execution service.
- Preview and sample verification paths also use the execution service.
- UI cancellation invokes the execution service once.
- The context-job layer records worker outcome but does not fail the same
  verification again.
- Preserve the existing staged -> bind -> expose implementation in
  `bind_and_expose_judgehost_runtime()`.

### Batch 2 tests

Unit tests:

- duplicate registration;
- identity-matched and stale unregistration;
- notification ordering and missing-runtime no-op;
- registry lock scope does not execute external work.

Service tests:

- cancellation before, during, and after registration;
- scheduler exception and activation failure;
- completion commit precedes successor notification;
- SQLite failure prevents Judgehost drain.

E2E tests:

- the public preview route drives a sample verification through the same
  execution service and materializes its input/answer refs before compiling;
- an immediate mock Judgehost callback;
- public cancellation;
- a complete verification workflow;
- callback/cancellation races;
- a cancelled verification cannot be revived by coordinator startup.

### Batch 2 acceptance

- Judgehost dispatch has no verification-scheduler import.
- Workspace has no coordinator register/unregister operation.
- No reference to the deleted global registry functions remains.
- All four groups pass in a Linux virtualenv. CI uses its Ubuntu jobs; the
  recorded delivery audit used the isolated container command above.
- Local Docker mock Judgehost E2E passes.
- Execution, verification, and Judgehost documentation describes the new
  current ownership.
- Keep the removed global-runtime finding closed. Narrow `PLC-006` only to the
  verification models and stores that remain direct Judgehost dependencies.

Out of scope:

- Moving the complete `verification_dag.py` module.
- Splitting `app/service/judgehost/result.py`.
- Moving RuntimeConfig or changing SQLite schema, wire payloads, task identity,
  program identity, cache identity, canonical completion, or late diagnostics.

## Batch 3: Judgehost result pipeline convergence

This batch has three responsibility steps. It reduces the responsibility
density of the Judgehost result processor while preserving `/api/v4/*`, JSON
integer `1`, scheduler claims, and late-diagnostic behavior. All three steps
were delivered in the combined runtime/result commit listed above.

### 3A. Public batch-finalization boundary

Review boundary:

```text
Separate Judgehost batch finalization
```

Implementation:

- Extract `JudgehostBatchFinalizer`.
- Give it batch/program terminal checks, final lease release, retry handling,
  and finalization coordination.
- Inject this boundary into dispatch and the Judgehost API.
- Construct the finalizer and both publishers once in the Judgehost composition
  root; inject a narrow finalization port into callback and dispatch
  orchestration instead of constructing it in `ResultProcessor`.
- Remove calls from dispatch and API to private ResultProcessor finalization
  methods.
- Keep case, program, and batch state transitions owned by BatchScheduler.

Invariants:

- Scheduler lock scope produces only immutable claims and snapshots.
- SQLite, blob storage, completion sinks, and callbacks execute outside the
  scheduler lock.
- Result normalization does not change in this commit.

### 3B. Canonical case-result normalization

Review boundary:

```text
Normalize Judgehost case results
```

Extract dependency-light captured-case models and
`normalize_captured_case()`.

Inputs:

- test/input identity and interactive mode;
- the raw final run result, fallback runtime, score, and canonical run config;
- already captured artifact bytes and refs;
- an optional parsed pass bundle, capture warning, and active debug text.

Outputs:

- the canonical case `ExecutionResult` used by the scheduler;
- the normalized run result and verdict;
- normalized runtime, CPU, wall, memory, and score fields.

The normalizer must not access SQLite, write blobs or caches, operate the
BatchScheduler, notify a coordinator, or construct an HTTP response.

It covers final-run RE/FL/WA/OK classification, interactive and multi-pass
execution, missing metadata, output and time limits, incomplete pass bundles,
capture warnings, resource usage, debug feedback, and complete artifact-ref
preservation. Compile failure/CE arrives through `update-judging` without a
final run callback; scheduler helpers construct its canonical result and the
batch finalizer owns orchestration and publication. Expected-RE matching remains
a verification-completion responsibility. Toolchain telemetry is captured by
callback orchestration and is not a normalizer input.

Artifact ingestion remains an orchestration step:

```text
validate and claim
  -> ingest artifacts
  -> normalize result
  -> commit scheduler decision
  -> persist verification completion
```

### 3C. Completion and diagnostic publication

Review boundary:

```text
Separate Judgehost result publication
```

Introduce `CaseCompletionPublisher` and `CaseDiagnosticPublisher`.

Completion publication:

- Sends scheduler-decided terminal cases to the verification completion sink.
- Marks `completion_acknowledged` only after durable persistence succeeds.
- Treats first completion and valid idempotent retries as success.
- Leaves the case unacknowledged and returns a non-2xx result when the sink or
  SQLite write fails.
- For a custom-run case without `verification_task_id`, acknowledges the
  scheduler's captured terminal decision without invoking the verification
  completion sink.

Diagnostic publication:

- Diagnostics before decision capture are included in the canonical result.
- Diagnostics after capture but before completion remain pending in memory.
- Diagnostics after completion update only the late-diagnostic snapshot.
- Pending diagnostics prevent quiet cleanup until persisted.
- Diagnostic persistence does not notify the coordinator.

The final callback sequence is:

```text
callback admission
  -> acquire immutable case receipt
  -> validate owner and generation
  -> scheduler claim
  -> artifact ingestion
  -> result normalization
  -> scheduler decision
  -> durable verification completion (only when verification_task_id is set)
  -> completion acknowledgement
  -> batch finalization
  -> release receipt
  -> HTTP JSON 1
```

Failure behavior:

- A busy claim remains retryable with a non-2xx response.
- Owner or generation mismatch remains non-2xx.
- Artifact, sink, or SQLite failure is non-2xx so judgedaemon retries.
- Valid callbacks for already terminal, cancelled, or cleaned cases are
  acknowledged as no-ops.
- An ordinary duplicate final result never becomes a late diagnostic.

### Batch 3 tests

Unit tests:

- RE/FL/WA/OK, output-limit, interactive, missing-metadata, and multi-pass
  final-run normalization matrices;
- artifact, capture-warning, debug-feedback, and usage round trips;
- compile/CE construction through batch-finalizer tests;
- batch-finalizer state transitions;
- diagnostic deduplication and bounded-size eviction.

Service tests:

- CE versus final-result races;
- internal-error versus final-result races;
- receipt versus quiet-cleanup ordering in both directions;
- completion-persistence failure and retry;
- pending diagnostics blocking cleanup;
- duplicate callback persistence exactly once;
- valid ACK/no-op after in-memory case loss on restart.

E2E tests:

- `/api/v4` authentication and complete lease/fetch/report flow;
- compile failure and expected RE;
- active internal-error;
- late add-debug-info and late internal-error;
- duplicate callback;
- maintenance admission;
- JSON integer `1` acknowledgement.

### Batch 3 acceptance

- All four test groups pass in a Linux virtualenv. CI uses its Ubuntu jobs; the
  recorded delivery audit used the isolated container command above.
- Local Docker mock Judgehost E2E passes.
- The normalizer has no SQLite, runtime-config, or registry import.
- The result processor does not construct batch finalization or publication
  services. It invokes injected ports and the dependency-light normalizer while
  retaining callback validation and artifact-capture orchestration.
- Judgehost, execution, and persistence documentation reflects the current
  implementation, and `PLC-007` is closed or narrowed to its remaining facts.

Out of scope:

- HTTP route, Judgehost wire, or SQLite schema changes.
- Artifact owner indexing.
- Task, program, or cache identity changes.
- Moving the `ExecutionResult` package.
- Complete RuntimeConfig separation.
- Source/config canonicalization.

## Work after these batches

After all three batches are complete and independently verified, the next
planning cycle covers Runtime/config separation, `STO-003`, `STO-005`,
`STO-009`, source canonicalization, export admission deduplication, and the
remaining ownership findings.

Do not automatically push these commits unless explicitly requested.
