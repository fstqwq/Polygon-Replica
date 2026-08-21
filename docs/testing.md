# Testing policy

The owning product, protocol, or service document defines intended behavior. A
test provides executable evidence for a selected consequence of that behavior;
it does not create the contract and does not prove the implementation correct.
When intended behavior changes, obsolete tests change or disappear with it.

## Selecting tests

Add or retain a test only when all of the following are true:

- the behavior is stable and intended;
- the suite can observe a deterministic consequence;
- the test would fail if that behavior were broken; and
- the regression risk justifies the maintenance cost.

High-value subjects are external HTTP, judgehost, package, storage, and security
contracts; durable state; user-visible side effects; failure, ordering,
concurrency, and cleanup invariants; and composition through a real entry path
where a narrower test can remain green while the product is broken.

Use this default for a change:

| Change | Default verification |
| --- | --- |
| External protocol, security boundary, durable transition, or concurrency invariant | Add or update the owning behavioral test. |
| User workflow with a durable or externally visible result | Exercise one real entry-path scenario; update an existing owner before adding another. |
| Bug fix | Add a regression test only when the violated behavior is stable and the failure has a credible recurrence path. |
| Copy, spacing, color, CSS, markup arrangement, or other presentation-only work | Review the rendered UI; do not add an automated assertion by default. |
| Refactor, code movement, or dead-code removal | Rely on the existing suite and static checks unless a previously untested boundary is exposed. |

Coverage, test counts, code churn, and review comments are not reasons by
themselves to add a test. An uncovered branch can be dead code that should be
deleted. A completed fix does not automatically need a permanent regression
test.

## Keeping and retiring tests

A permanent test must have an owner, an observable contract, and a distinct
failure that it detects. State all three without referring to the commit that
introduced the test:

- the product, protocol, or service document that owns the behavior;
- the externally visible or durable outcome being observed; and
- the credible implementation regression that can break this test while the
  other retained tests still pass.

If one of these cannot be identified, delete the test. A test is also a removal
candidate when it duplicates an owning scenario, only detects a helper rewrite,
asserts presentation that has no defined contract, or needs disproportionate
fixture setup to observe an incidental detail. Fixing a bug explains why a test
was introduced; it does not give that test permanent status.

When a contract changes, replace the old expectation instead of preserving both
histories. Do not assert that a removed table, column, route, field, compatibility
shape, or implementation symbol remains absent. Assert the current behavior that
would be wrong if the obsolete shape were accidentally used. Deleted history is
not a second public contract.

During a suite audit, review tests individually rather than retaining an entire
module by category. Keep the smallest scenario that owns each behavior, move a
shared domain invariant to its service test, and delete the weaker duplicates.
Do not rewrite a test into a tautology merely to preserve its name or count.

## UI assertions

Most UI changes do not require a new assertion. A UI test performs a meaningful
user action through the real route or interaction boundary and observes the
result outside the presentation code: authorization, response status or
redirect, cookies, Git/SQLite/files, queued work, downloadable outputs, or a
user-visible capability.

For example, a settings test submits the form and reads the saved configuration.
It does not separately assert that every label, CSS class, input, and success
message exists. A scenario that already uses a control does not also need a
presence assertion for that control.

Do not normally lock template fragments, CSS classes, DOM order, element
counts, incidental wording, internal read-model keys, helper calls, or complete
HTML output. An accessibility attribute, client/server selector, form field, or
exact response body may be asserted only when it is itself a stable interface.
Keep one owning assertion and name the behavior it protects. Visual layout and
copy quality are review concerns unless the product defines a precise
presentation or accessibility contract.

Do not use substring, regular-expression, element-count, or source-order
assertions over rendered HTML as evidence that a UI capability exists. Finding
a label, link, form action, CSS class, status word, or template fragment proves
only that the server emitted those bytes; it does not prove that the action is
authorized, accepted, persisted, executed, or retrievable. Exercise the action
and observe its durable or externally visible result instead. Delete a UI test
whose only outcome is matching presentation strings. When a behavioral UI test
also contains such assertions, remove the presentation checks and retain the
smallest action-to-outcome journey.

Rendered-output assertions remain appropriate when the bytes themselves are
the contract, such as escaping untrusted values, preventing sensitive-data
disclosure, generating a downloadable document, or returning an external
protocol payload. Name that contract explicitly and avoid mixing it with
incidental page copy or layout.

A UI workflow should have a small number of high-value entry-path scenarios.
Shared domain behavior belongs in its owning service or protocol test instead
of being repeated on every page. When a UI fix changes an existing scenario,
replace its obsolete expectation or simplify the scenario; do not append one
assertion per fix or restate setup facts after the outcome is demonstrated.

Resource classification does not establish test value. A test may belong to the
`e2e` resource group because it loads the global application or invokes a public
route, while still being a low-value presentation assertion that should be
deleted. Conversely, an end-to-end scenario is justified when crossing the real
boundary is essential evidence: routing and authorization reach the intended
service, a submitted operation changes durable state, an asynchronous workflow
reaches its terminal result, or a derived output can be retrieved through its public
contract.

For a user capability, normally keep one representative successful journey and
only the failure cases that protect distinct authorization, atomicity, security,
or recovery behavior. Do not create one E2E test per page, control, status label,
historical bug, or rendering branch. Loading a page and finding a sentence is not
an end-to-end product outcome.

## Assertion discipline

A test name states one behavior. Its assertions cover the primary observable
outcome and only the negative outcomes needed to distinguish the regression.
Assertions about intermediate helper calls, mock choreography, or internal data
shapes are appropriate only when that boundary is the contract under test.

Verify the world rather than the application's self-report. Re-read the stored
row or file, inspect the issued cookie, retrieve the derived output, or observe the
terminal job state. A success message alone does not prove that an action took
effect. For a failure test, also verify that protected state did not change when
that is the important invariant.

## Test boundaries

Use the least expensive layer that can observe the contract, but do not replace
the real implementation with mocks merely to make an assertion convenient.
Mock external or nondeterministic boundaries such as networks, clocks, and host
tools; keep route-to-service-to-storage behavior real when that composition is
what matters.

Exact snapshots are reserved for stable external surfaces whose complete shape
is the contract. Pin a repeated surface once and use narrower behavioral checks
elsewhere. Normalization removes genuine volatility; it must not erase the
behavior the test claims to protect.

Generated archives are not byte snapshots unless an external protocol explicitly
defines their complete serialization. Package tests compare member paths, payloads,
and required metadata rather than compression streams, entry order, timestamps,
central-directory bytes, or whole-archive equality. Archive checksums are asserted
only when the behavior under test is integrity validation or reuse of the same
published artifact.

When touching a brittle test, remove redundant implementation assertions and
leave the smallest scenario that proves the intended behavior. Before keeping a
new assertion, verify that breaking the named behavior makes it fail; an
assertion that only notices an implementation rewrite is not protection. A
broad rewrite of unrelated tests is not required. Resource classification and
CI execution are documented in
[the test resource groups](../tests/RESOURCE_GROUPS.md).

## Judgehost Docker E2E

`tests/scripts/docker-e2e.sh` creates a new Compose project, image tag, and named
volumes for each run. CI passes it the already built application image; a local
run builds and later removes its own image. It first compiles real `pdflatex`
and `xelatex` PDFs through the production `TexCompileService` and bubblewrap
backend in a networkless one-shot container; this checks both image formats and
the production root-switch path rather than only the presence of packages.
Network isolation here belongs to the one-shot container; it is not a claim
that the TeX backend itself unshares the network.

The mock exercises registration, work lease, file download,
version report, compile report, and final-result exchange without executing
untrusted programs or starting a real judgehost. Its fixture covers successful
results, CE through `update-judging`, RE, active internal-error, idempotent final
ACKs, and retry-deduplicated late debug/internal diagnostics. Application, mock,
and result runner share an internal-only Compose network and publish no host
port. The runner first invokes the public Preview route, observes its sample
verification and exact materialized input/answer refs, and checks that TeX read
the rendered sample files before producing a PDF. It then runs a full
verification and observes immutable canonical task results, generated
input/answer blobs, and the separate late-diagnostic snapshot through persisted
state. The mock is a project-owned protocol fixture; the test does not clone or
approve an upstream source tree.

`tests/scripts/e2e-real.sh` runs the deployed authoring journey against the
official `domjudge/judgehost:9.0.0` image. CI does not compile DOMjudge. The
journey performs first-run setup, judgehost configuration, problem creation,
and every fixture file save through public HTTP and the latest Polygon Agent
CLI checkout. Before execution, it installs the previous
`build.json` shape through the CLI, opens the authoring workspace, and requires a
Review and Publish warning plus a canonical file that preserves the current
selections. It then sends a generated input larger than one MiB through the real
judgehost multipart callback twice: the rejected form must retain the
validator's explicit diagnostic on the Verification details page, and the
corrected form must persist the complete input. Before the successful run, the
journey cancels one active Verification and runs an all-AC `tle_or_re` solution
that must fail only at the program-level requirement. The final real execution
covers AC, WA, TL, RE, CE, AC/TL, AC/RE, and AC/WA solution verdict patterns,
the accepted answer, and public derived-output downloads. The journey asserts
the resulting sample/ok, all/failed, all/cancelled, all/failed, and all/ok
history before it continues through commit, DOMjudge and ICPC
2025-09 Package Exports, direct Native Package download, Contest
creation and statement/package builds, role-aware page walk, concurrent
Alice/Bob conflict resolution, maintenance cleanup, backup, and restart.

The deployed journey registers an outsider, a reader, and two writers. A
role-aware page walk visits the stable HTML routes as anonymous,
outsider, reader, writer, and owner/system-administrator actors. It checks
access outcomes and rejects unexpected server errors without pinning sentences,
CSS selectors, or incidental markup. Pages that require a runtime identity are
exercised by the workflow that creates that identity.

Finally, Alice and Bob start with separate workspaces on the same published
revision and edit the same path. Alice publishes first. Bob's stale publish must
leave published Git and his local edit unchanged; Bob then resolves the conflict
through the public merge review and apply endpoints and publishes a linear child
of Alice's revision. The assertions cover Git ancestry, workspace isolation,
and selected file bytes rather than merge-page presentation.

`tests/scripts/e2e-domserver-900.sh` is a separate package-consumer journey. It
starts MariaDB 10.11, `domjudge/domserver:9.0.0`, one official judgehost for
Polygon Replica, and one official judgehost for DOMserver. The latest Agent CLI
creates and publishes pass-fail, interactive-only, and multi-pass-only fixtures.
Polygon Replica verifies each published revision and exports a DOMjudge ZIP.
The controller imports each ZIP through the DOMjudge API while the admin user is
bound to a jury team. DOMserver therefore creates the package's jury submissions
itself; its judgehost executes them, and the controller checks the real results
and runs DOMjudge's Judging verifier. The only accepted import danger messages
are the three exact 9.0.0 mixed-directory annotation mismatches.

Package structure, PPF 2025-09, combined interactive/multi-pass, cache behavior,
warning propagation, and import round-trip are contract tests rather than part
of the DOMserver journey. All Docker test definitions live in
`tests/docker_e2e/`. The mock, deployed product, and DOMserver journeys run as
independent jobs in the `Docker E2E` workflow with a 15-minute outer limit.
That workflow runs on a push unless every changed path is in its explicit
non-Docker-input allowlist. The allowlist contains static assets, templates,
pure presentation projections, documentation, ordinary Python tests and their
Fast CI-only resource and harness files. Changes below `tests/docker_e2e/` or
to `tests/scripts/docker-e2e.sh`, `tests/scripts/e2e-real.sh`, and
`tests/scripts/e2e-domserver-900.sh` still run all three Docker jobs. An
unknown path or a push that mixes ignored and non-ignored changes also runs all
three jobs. A manual `workflow_dispatch` always runs the workflow. There is no
scheduled Docker E2E run.

The separate `Fast CI` workflow runs static checks and the unit, service,
executor, and ordinary E2E resource groups on every push that is not exclusively
documentation. Documentation-only pushes under `docs/` or containing only
Markdown files do not start either CI workflow. A push that mixes documentation
and code still runs the workflows selected by the changed code paths. Both
workflows cancel an older in-progress run for the same ref when a newer push
supersedes it.

Run the isolated E2E from the repository root:

```bash
bash tests/scripts/docker-e2e.sh
bash tests/scripts/e2e-real.sh
bash tests/scripts/e2e-domserver-900.sh
```
