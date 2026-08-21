# Testing policy

The owning product, protocol, or service document defines intended behavior. A test records executable evidence for a consequence of that behavior; it does not create the contract. When the contract changes, obsolete tests change or disappear with it.

## Selecting tests

Add or retain a test when the behavior is stable, the consequence is deterministic, the test would fail if the behavior regressed, and the regression risk justifies its maintenance cost.

Priorities are external protocols, security boundaries, durable transitions, user-visible side effects, failure and ordering invariants, concurrency, cleanup, and real entry paths. Coverage, test counts, and code churn are not reasons to add a test.

| Change | Default verification |
| --- | --- |
| External protocol, security boundary, durable transition, or concurrency invariant | Add or update the owning behavioral test. |
| User workflow with a durable or external result | Exercise one real entry path; update its existing owner before adding another. |
| Bug fix | Add a regression test only when the behavior is stable and can credibly recur. |
| Presentation-only change | Review the rendered UI; do not add an automated assertion by default. |
| Refactor or dead-code removal | Use the existing suite and static checks unless a new boundary is exposed. |

## Keeping and retiring tests

Every permanent test needs an owning document, an observable outcome, and a distinct regression it detects. Delete it when the owner or regression cannot be named, when it duplicates a stronger scenario, or when it only protects an implementation detail.

When a contract changes, replace the old expectation. Do not preserve removed tables, routes, fields, compatibility shapes, or implementation symbols as negative assertions. Keep the smallest scenario that owns each behavior and delete weaker duplicates.

## UI assertions

A UI test should perform a real action through the route or interaction boundary and observe authorization, response status or redirect, cookies, durable state, queued work, or a retrievable output. A label, CSS class, status word, template fragment, or success message is not evidence that the capability worked.

Do not normally assert template fragments, incidental wording, CSS classes, DOM order, element counts, or complete HTML. Assert rendered bytes only when the bytes are the contract, such as escaping untrusted values, preventing disclosure, generating a document, or returning an external protocol payload.

Keep one representative success journey and only the failure cases that protect distinct authorization, atomicity, security, or recovery behavior. Do not create one E2E test per page, control, status label, historical bug, or rendering branch.

## Assertion discipline

A test name states one behavior. Assertions cover its primary observable outcome and the negative state needed to distinguish the regression. Verify the world rather than the application's self-report: reread the stored row or file, inspect the issued cookie, retrieve the derived output, or observe the terminal job state.

Use mocks only at external or nondeterministic boundaries such as networks, clocks, and host tools. Keep route-to-service-to-storage behavior real when that composition is the contract. Exact snapshots are reserved for stable external surfaces whose complete shape is defined.

Generated archives are not byte snapshots unless an external protocol defines their complete serialization. Package tests compare member paths, payloads, and required metadata. Assert archive checksums only when testing integrity validation or reuse of a published artifact.

## Test boundaries

Use the least expensive layer that can observe the contract. Shared domain behavior belongs in its service or protocol test; a UI test should not repeat it through every page. Resource classification does not establish test value: an E2E test is justified when the real boundary is essential evidence for routing, authorization, durable state, asynchronous completion, or a public derived output.

## Judgehost Docker E2E

`tests/scripts/docker-e2e.sh` creates an isolated Compose project, image tag, and named volumes for each run. It first exercises the production TeX renderer in a networkless one-shot container, then runs a mock judgehost journey covering protocol callbacks, preview sample evidence, full verification, durable results, and generated outputs.

`tests/scripts/e2e-real.sh` runs the authoring workflow against the official DOMjudge judgehost image. It covers setup, workspace editing, verification, cancellation, native and external packages, contest workflows, access control, maintenance, backup, restart, and stale-workspace conflict resolution.

`tests/scripts/e2e-domserver-900.sh` imports a generated DOMjudge package into DOMserver and lets its judgehost execute the resulting submissions. Package structure, ICPC 2025-09, combined interactive/multi-pass, cache behavior, warning propagation, and import round-trip remain focused contract tests rather than DOMserver journey details.

The three Docker journeys run as independent jobs with a 15-minute outer limit. Changes below `tests/docker_e2e/` or to their runner scripts run all three jobs; documentation-only changes do not start either CI workflow. Fast CI covers static checks and the unit, service, executor, and ordinary E2E resource groups for non-documentation pushes.

Run the isolated journeys from the repository root:

```bash
bash tests/scripts/docker-e2e.sh
bash tests/scripts/e2e-real.sh
bash tests/scripts/e2e-domserver-900.sh
```
