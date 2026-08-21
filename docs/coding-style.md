# Python coding and import policy

The [PEP 8 import guidance](https://peps.python.org/pep-0008/#imports) is the baseline unless this repository defines a narrower rule.

Application code targets CPython 3.14 exclusively and uses its native deferred
annotation semantics.

## Incomplete and legacy data

Authoring reads must remain usable with incomplete source. Normalize or diagnose the condition once at its owning boundary:

- authoring reads return diagnostics and a complete page model, using neutral
  defaults only for display and editing;
- a safe, mechanical repair may run there when it preserves every current
  field and does not infer user intent;
- ambiguous data stays unchanged and is shown as a warning under Review and
  Publish;
- Verification, package export, native package construction, and contest
  builds validate the complete canonical source before doing work, then pass
  only that canonical shape internally;
- a consumer may reject, explicitly upgrade, or use a defined fallback at its
  entrance, but code behind that entrance does not see incomplete or legacy
  shapes.

Automatic repair is limited to changes that preserve current authored intent. A repair remains an ordinary workspace change for review and publication. Ambiguous source stays unchanged.

## Import layout

Imports stay at module scope after the docstring and are grouped as standard-library, third-party, and repository-local. Separate `import module` statements; parenthesized `from` imports may list names vertically.

Use absolute imports for repository modules. A local import requires an initialization cycle, optional dependency, or intentional import timing, with the reason visible at the import site.

Aliases are reserved for established external conventions or module-name collisions. Symbols in `from` imports are never renamed.

Every import has a visible consumer. Prefer an explicit registration call over
an otherwise-unused import whose only purpose is an import-time side effect.

## Public boundaries

A leading underscore marks a non-public name. Cross-package callers use the owning public boundary. Promote or move a needed operation instead of chaining private imports.

Private imports are allowed within one package hierarchy and rejected across unrelated application packages. Forwarding modules do not bypass this rule.

Re-exports are explicit. Application packages do not synthesize exports through `globals()`, `dir()`, import loops, or wildcards.

Persistence implementations under `app.service.disk` and `app.service.memory` are private storage boundaries. Their import allowlist records current owners and should shrink as boundaries improve.

## What the checks enforce

The default import-policy gate scans Python files in `app/`, `tests/`, and
`scripts/` and rejects:

- wildcard imports;
- `from X import Y as Z`;
- mesh-style relative imports such as `from .module import name`;
- dynamic application re-export chains;
- dynamic `__all__` outside package initializers;
- imported names exposed through `__all__` outside `__init__.py`; and
- assignments to `_` that exist only to suppress unused-name checks; and
- `*args` or `**kwargs` on application business operations, where an exact
  typed signature must reject unknown arguments.

Variadic parameters remain valid only on explicitly identified framework
adapters, generic call adapters, and command wrappers whose actual contract is
forwarding an arbitrary argument sequence. Ordinary facades, services, and
handlers do not use variadic parameters to absorb misspelled or obsolete
inputs.

Static checks scan every Python file below `app/`, `tests/`, and `scripts/`. They enforce cross-package privacy, persistence allowlists, the `route -> impl -> service` dependency direction, and an acyclic `app` graph. CI also runs pyflakes, pylint, and mypy. Import grouping and placement remain review rules because no import sorter is configured.

On the configured Linux test environment, run:

```bash
python -m pip install -r requirements.txt -r requirements-static.txt
bash tests/scripts/check-import-policy.sh
```

The same gate is included in `tests/scripts/check.sh`. Update the checker and this document together when an enforced rule changes; do not duplicate a static rule in a UI or unit test.
