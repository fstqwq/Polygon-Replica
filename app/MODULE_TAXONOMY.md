# Module Taxonomy

## Canonical Layers

- `app/routes`: HTTP route registration only.
- `app/impl`: request/page orchestration and capability handlers.
- `app/services`: domain logic and reusable business workflows.
- `app/service/platform`: infrastructure/runtime primitives (fs, queue, artifacts, system).

## Canonical Roles

Allowed role-oriented filenames:

- `api.py`: public domain entrypoints.
- `command.py`: mutating workflow operations.
- `query.py`: read projections/context assembly.
- `policy.py`: validation/normalization/business rules.
- `adapter.py`: external format/protocol bridges.
- `store.py`: persistence/storage access.
- `model.py`: domain data model definitions.
- `runtime.py`: runtime/wiring glue.
- `errors.py`: error taxonomy and adapters.
- `paths.py`: path/layout safety and resolution.

## Naming and Boundary Rules

- New/refactored modules MUST map to one layer and one role.
- New files MUST NOT use ambiguous bucket names such as `deps.py` or generic `common.py`.
- Cross-package private imports are forbidden (`from app.<x> import _private_symbol`).
- Shim-only re-export modules are forbidden in migrated scope.
- In-scope service naming MUST NOT use `_service` suffixes in file/module/symbol names.

## Temporary Bridge Policy

Temporary bridges are only allowed when both are true:

1. A same-change removal task exists.
2. The bridge file includes explicit TODO removal marker with task id.

Otherwise bridge files are non-compliant.

## Acceptance Checklist

- Mapping table committed (`old -> new`, action, rationale).
- Anti-pattern scans captured (private imports, shims, forbidden names).
- Placeholder/task markers cleaned from migrated modules.
- WSL test run recorded after sync.
