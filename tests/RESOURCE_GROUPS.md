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

The pre-push hook runs only the resource manifest and group-contract checks. It
does not load or execute tests.

| Group | Allowed resources |
| --- | --- |
| `unit` | Pure Python and small temporary files; no runtime config, SQLite, Git, workers, or subprocesses |
| `db` | Template-restored SQLite and small temporary files; no global runtime config, Git, workers, or subprocesses |
| `workspace` | SQLite plus lazily-created real Git repositories; no worker submission |
| `executor` | Compilers, shell scripts, bwrap, and TeX tools |
| `large-fixture` | One compatibility canary for each maintained real package format |
| `e2e` | Routes, templates, ACL, workers, and the complete runtime service graph |

Run one group:

```bash
PYTHONPATH="$PWD" bash tests/scripts/test.sh unit
```

Run all groups in separate Python processes:

```bash
PYTHONPATH="$PWD" bash tests/scripts/test.sh
```

Static checks are intentionally separate:

```bash
PYTHONPATH="$PWD" bash tests/scripts/check.sh
PYTHONPATH="$PWD" python -m pylint app
```

Each group writes `.test-results/<group>.json` with its total duration and
per-test timings. GitHub Actions runs static checks once and executes all six
groups as parallel matrix jobs.
