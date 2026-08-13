# Python coding and import policy

The [PEP 8 import guidance](https://peps.python.org/pep-0008/#imports) is the
baseline when this repository does not define a narrower rule. Repository
dependency boundaries take precedence over general style guidance.

Application code targets CPython 3.14 exclusively and uses its native deferred
annotation semantics.

## Incomplete and legacy data

An authoring system must remain usable when source is incomplete or was written
by an older application revision. Do not turn one invalid configuration file
into an Internal Server Error that prevents the author from opening and fixing
the problem. Also do not carry a partly interpreted value through the program
and add `try`/`except`, fallback values, or legacy branches to every consumer.

Handle the condition once at the boundary that owns it:

- authoring reads return diagnostics and a complete page model, using neutral
  defaults only for display and editing;
- a safe, mechanical repair may run there when it preserves every current
  field and does not infer user intent;
- ambiguous data stays unchanged and is shown as a warning under Review and
  Publish;
- Verification, Package Export, verified-revision construction, and Contest
  builds validate the complete canonical source before doing work, then pass
  only that canonical shape internally;
- a consumer may reject, explicitly upgrade, or use a defined fallback at its
  entrance, but code behind that entrance does not see incomplete or legacy
  shapes.

Automatic repair is deliberately narrow. Removing fields whose behavior was
deleted is safe; selecting a main solution from filenames or `.desc` files is
not. A repair is an ordinary workspace change that the author reviews and
publishes. Do not add hidden compatibility state or a parallel runtime model.

## Import layout

Imports normally stay at module scope after the module docstring. Separate them
into standard-library, third-party, and repository-local groups with one blank
line between groups. Put separate `import module` statements on separate lines;
a parenthesized `from` import may list several names vertically.

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

The checker discovers every Python file below `app/`, `tests/`, and `scripts/`
without a module list or boundary configuration. It derives application layers
from the canonical `app.route`, `app.impl`, and `app.service` package locations
and enforces the normal `route` to `impl` to `service` direction. The dependency-light
`app.config` package owns typed configuration definitions and immutable active
snapshots; implementation and service modules may depend on that foundation.
The application composition root is `app.runtime.ApplicationRuntime`.
`app.main.create_app()` installs one explicit instance in application state;
request implementation code reaches it only through the implementation
boundary accessor, while lifecycle and background work receive or capture it
explicitly. Verification workflow policy and execution are owned by
`app.service.verification`; service code must not import implementation
modules.

Cycle analysis covers every discovered module below `app/`. Any cycle fails the
gate, and newly added modules are included automatically.

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
