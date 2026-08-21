# Module taxonomy

This document defines the stable application layers and the roles used to name modules. The [application package map](src/README.md) describes current package ownership, while the [Python coding and import policy](coding-style.md) defines the enforced dependency rules.

## Application layers

- `app/route` registers HTTP routes and translates framework inputs into calls to `app/impl`.
- `app/impl` owns request and page orchestration, capability checks, and HTTP response construction.
- `app/service` owns reusable domain workflows and infrastructure adapters. Service code does not depend on routes, templates, static assets, or implementation modules.
- `app/config` owns typed configuration definitions and the immutable active configuration snapshot.
- `app/runtime.py` is the process composition root. `app/runtime_lifecycle.py` receives that runtime explicitly for startup and shutdown.

The normal dependency direction is `route -> impl -> service`. Shared application foundations may be used where their documented responsibility requires it. The import-policy checker discovers the complete graph below `app/` and rejects cycles and forbidden cross-boundary imports.

## Module roles

Prefer a name that identifies the module's primary responsibility:

- `api.py` for a public domain service boundary;
- `command.py` for mutating workflows;
- `query.py` for read projections or context assembly;
- `policy.py` for validation, normalization, and business rules;
- `adapter.py` for an external protocol or format bridge;
- `store.py` for persistence access;
- `model.py` for domain data shapes;
- `runtime.py` for runtime wiring or lifecycle coordination;
- `errors.py` for an error taxonomy;
- `paths.py` for path and layout safety;
- `constant.py` for a module consisting of domain constants;
- `types.py` for shared type definitions when `model.py` would be misleading.

Avoid generic buckets such as `helpers.py`, `common.py`, or `deps.py` when the contents have a more specific owner. A forwarding or re-export-only module is not a substitute for moving an operation to its real public boundary.

## Boundary rules

- A new or substantially refactored module belongs to one layer and has one primary role.
- Cross-package callers use the owning package's public boundary instead of underscore-prefixed implementation details.
- Services receive dependencies from the composition root; they do not locate the running application or import `app.impl`.
- Filesystem layout and path safety belong to `app/service/platform/fs/`. Domain services own the meaning and lifecycle of the locators they request from that boundary.
- Verification owns execution evidence and artifact authorization. HTTP implementation code translates its typed outcomes into responses without taking ownership of the stored evidence.
- Statement language discovery, rendering, preview caching, native package statement materialization, and external-package adaptation remain separate responsibilities. A caller passes one normalized language into the render workflow rather than rediscovering it in downstream modules.

When a boundary changes, update this taxonomy only if the stable layer or role changed. Exact routes, SQL columns, class names, and source-file inventories belong to their owning protocol or package documentation.
