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

## UI assertions

Most UI changes do not require a new assertion. A UI test performs a meaningful
user action through the real route or interaction boundary and observes the
result outside the presentation code: authorization, response status or
redirect, cookies, Git/SQLite/files, queued work, downloadable artifacts, or a
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

## Assertion discipline

A test name states one behavior. Its assertions cover the primary observable
outcome and only the negative outcomes needed to distinguish the regression.
Assertions about intermediate helper calls, mock choreography, or internal data
shapes are appropriate only when that boundary is the contract under test.

Verify the world rather than the application's self-report. Re-read the stored
row or file, inspect the issued cookie, retrieve the artifact, or observe the
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
volumes for each run. Before the mock Judgehost can contact the application, a
one-shot
contract container clones the official DOMjudge `9.0.1` tag, verifies its exact
peeled commit, and checks the endpoint, HTTP method and form encoding, required
fields and download shapes, and HTTP-success handling used by the mock against
`judge/judgedaemon.main.php`. It also checks the official meaning of the final
response plus debug/internal-error persistence against
`webapp/src/Controller/API/JudgehostController.php`. It then writes both source
digests into a scoped approval record, which the mock revalidates before every
request. In particular, the gate proves that the daemon's array-valued debug
upload is sent as `multipart/form-data`; the mock uses the same encoding.

The mock exercises the official-shaped registration, work lease, file download,
version report, compile report, and final-result exchange without executing
untrusted programs or starting a real Judgehost. Its fixture covers successful
results, CE through `update-judging`, RE, active internal-error, idempotent final
ACKs, and retry-deduplicated late debug/internal diagnostics. Application, mock,
and result runner share an internal-only Compose network and publish no host
port; only the one-shot source verifier has upstream network access. The runner
observes the terminal verification, immutable canonical task results, generated
input/answer blobs, and the separate late-diagnostic snapshot through persisted
state.

Run the isolated E2E from the repository root:

```bash
bash tests/scripts/docker-e2e.sh
```
