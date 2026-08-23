# Test Resource Groups

Assertion selection and UI test scope are defined by the
[testing policy](../docs/testing.md). This document owns only CI resource
classification and execution.

`tests/resource_groups.json` assigns every `test_*.py` module to exactly one
resource group. The runner rejects missing, stale, or duplicate assignments
before loading tests.

Enable the repository hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The pre-push hook runs the shared static checks, including the resource manifest
and group-contract checks, followed by the `prepush` test group. The `prepush`
group contains repository-wide contracts that are worth checking on every push,
including documentation-only pushes. Ordinary domain behavior remains in its
own resource group.

| Group | Allowed resources |
| --- | --- |
| `prepush` | The same resources as `unit`; only repository-wide contracts suitable for every push |
| `unit` | Pure Python and small temporary files; no runtime config, SQLite, Git, workers, or subprocesses |
| `service` | One owning component with explicit dependencies; SQLite, local files, Git, threads, and workers are allowed, but the global runtime and public HTTP/UI entry points are not |
| `executor` | One owning component using compilers, shell scripts, bwrap, systemd checks, or TeX tools; the global runtime and public HTTP/UI entry points are not allowed |
| `e2e` | A public HTTP, UI, agent, or Judgehost boundary, or a complete background workflow through workers and durable completion. Classification describes resources, not whether a test deserves to exist. |

Reference-package compatibility canaries belong to `service`; fixture size does
not by itself define an execution boundary. A service test may use Git as its
storage adapter, but host compilers, sandbox tools, and TeX belong to
`executor`. Tests that load `app.main`, the global runtime config, TestClient,
or the shared full-runtime fixture belong to `e2e`.

An `e2e` assignment is not permission to pin incidental HTML. Retain a test in
this group only when the public entry path is necessary to observe the behavior;
otherwise move the invariant to its owning service test or delete a duplicate.
The selection and retirement rules remain those in the testing policy.

Run one group:

```bash
PYTHONPATH="$PWD" bash tests/scripts/test.sh unit
```

Run all groups in separate Python processes:

```bash
PYTHONPATH="$PWD" bash tests/scripts/test.sh
```

The shared static check includes the Linux-target mypy gate for the complete
`app/` tree:

```bash
PYTHONPATH="$PWD" bash tests/scripts/check.sh
PYTHONPATH="$PWD" python -m pylint app
```

Each group writes `.test-results/<group>.json` with its total duration and
per-test timings. GitHub Actions runs the pre-push hook on every push. A separate
workflow executes `unit`, `service`, `executor`, and `e2e` as parallel matrix
jobs for non-documentation pushes.
