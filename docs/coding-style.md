# Python coding and import policy

The [PEP 8 import guidance](https://peps.python.org/pep-0008/#imports) is the
baseline when this repository does not define a narrower rule. Repository
dependency boundaries take precedence over general style guidance.

## Import layout

Imports normally stay at module scope, after the module docstring and any
`from __future__` imports. Separate them into standard-library, third-party,
and repository-local groups with one blank line between groups. Put separate
`import module` statements on separate lines; a parenthesized `from` import may
list several names vertically.

Use absolute imports for repository modules. A function-local import is
reserved for a real initialization cycle, an optional dependency, or behavior
whose import timing is intentional; leave the reason visible at the import
site. Do not use a local import merely to conceal a dependency.

Aliases are acceptable for established external conventions such as
`import numpy as np`, or where a module-name collision would otherwise make the
code unclear. The repository does not permit renaming a symbol in a `from`
import.

Every import has a visible consumer. Prefer an explicit registration call over
an otherwise-unused import whose only purpose is an import-time side effect.

## Public boundaries

A leading underscore marks a non-public name. Code outside the owning package
imports its public service or implementation boundary instead of reaching into
underscore-prefixed helpers. If no suitable public operation exists, promote
or move the operation rather than creating chains of private imports.

The static check permits a private import within the same package and between
an ancestor and descendant package. It rejects an absolute `from app...`
import of an underscore-prefixed name across unrelated application packages.
Do not add a forwarding module whose only purpose is to wrap such a private
import.

An imported name does not become a public API merely because it is available
as a module attribute. Re-exports are explicit and intentional; application
packages do not synthesize exports through `globals()`, `dir()`, or import
loops. Wildcard imports are not used.

The persistence implementations under `app.service.disk` and
`app.service.memory` are private storage boundaries. Only the explicitly
allowed service owners import them; other modules consume the corresponding
public service API. The allowlist is enforced by the public-contract check and
should shrink when ownership is improved, not grow to bypass the boundary.

## What the checks enforce

The default import-policy gate scans Python files in `app/`, `tests/`, and
`scripts/` and rejects:

- wildcard imports;
- `from X import Y as Z`;
- mesh-style relative imports such as `from .module import name`; and
- dynamic application re-export chains.

The broader static check also rejects cross-package private imports and simple
forwarding shims in `app/`. The full public-contract suite enforces the
allowlist for direct imports of `app.service.disk` and `app.service.memory`.
CI also runs pyflakes over `app/`, `tests/`, and `scripts/`, and pylint over
`app/`; unused or unresolved imports are lint failures.

The repository wrapper loads `import-policy/import-boundaries.json`. Its layer
allowlists apply to all route, implementation, and service modules. They
enforce the normal `route` to `impl` to `service` direction and prevent those
layers from importing templates or static assets. The dependency-light
`app.config` package owns typed configuration definitions and immutable active
snapshots; implementation and service modules may depend on that foundation.
The service layer has two bounded placement exceptions:
`app.impl.runtime.config` and `app.impl.workspace.verification_dag`. They are
current debt, not permission to add other service-to-implementation imports.

Cycle and naming checks use a staged module set because the complete current
application graph still contains known reverse dependencies. The checked set is
`app.route`, `app.impl.auth`, `app.impl.run_export`, `app.impl.workspace`, and
`app.service.statement`. Cycles wholly inside that set fail the gate. Expand
the set when a package has a clear owner and is cycle-free; do not add a
baseline or hide a new cycle.

Within the selected implementation and service packages, naming analysis
rejects plural package/module segments except for narrow lexical exceptions,
and rejects packages that accumulate three or more modules repeating the
package name as a prefix or suffix. These checks keep a domain package from
turning into a flat cluster such as `statement_parse`, `statement_render`, and
`statement_service`; use responsibility-bearing module names instead.

Import grouping, placement, and the preference for absolute imports are
authoring and review rules. The repository does not currently run an import
sorter, so passing the static gate alone does not prove that layout is correct.

On the configured Linux test environment, run:

```bash
bash tests/scripts/check-import-policy.sh
```

The same check is included in `tests/scripts/check.sh`, which also runs the
private-import and forwarding-shim checks. When an enforced rule changes,
update the checker and this document together. Do not add a UI or unit test
that merely restates the static import checker.
