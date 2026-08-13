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

High-value subjects are external HTTP, Judgehost, package, storage, and security
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
untrusted programs or starting a real Judgehost. Its fixture covers successful
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

`tests/scripts/e2e-real.sh` adds the deployed authoring journey. It builds the
application image once, pulls the official `domjudge/judgehost:9.0.0` and
`domjudge/judgehost:bleeding` images, and starts two isolated Compose projects
in parallel. CI does not compile DOMjudge. Each project has its own application,
database, filesystems, network, Judgehost credentials, daemon id, and run-user
id. A result is therefore attributable to exactly one Judgehost image. The
script logs the pulled image digests for reproducibility; `bleeding`
intentionally remains a floating upstream compatibility target.

Both projects perform first-run setup, Judgehost configuration, problem
creation, and every fixture file save through public HTTP and the latest
Polygon Agent CLI checkout. Before execution, they install the previous
`build.json` shape through the CLI, open the authoring workspace, and require a
Review and Publish warning plus a canonical file that preserves the current
selections. They then run the same generated test through a real Judgehost and
observe generated input, accepted answer, AC, WA, CE, and public derived-output
downloads. The `bleeding` project alone continues through the more expensive
product tail: sample preview, commit, Native and ICPC exports, contest creation
and statement PDF export, role-aware page walk, and concurrent Alice/Bob
conflict resolution. This avoids repeating work that does not vary by
Judgehost implementation.

The deployed journey on `bleeding` then registers an outsider, a reader, and
two writers. A role-aware page walk visits the stable HTML routes as anonymous,
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

Run the isolated E2E from the repository root:

```bash
bash tests/scripts/docker-e2e.sh
bash tests/scripts/e2e-real.sh
```
