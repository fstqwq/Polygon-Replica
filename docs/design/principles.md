# Design principles

Polygon-Replica uses a small number of explicit authorities:

- Git owns committed problem source history.
- SQLite owns durable identity, metadata, configuration, and job summaries.
- Configured filesystem roots own large payloads and derived products.
- The web application owns authoring orchestration and authorization.
- Authenticated Judgehost deployments are trusted execution workers.

Mutable workspace state and immutable published revisions are kept distinct.
Artifacts are addressed by locators rather than embedded in relational rows.
Cleanup may invalidate cleanup-safe locators without deleting their durable
summary rows. Current availability must therefore be checked at access time.

Validation and normalization happen at transport, archive, or storage
boundaries. Internal services consume canonical values and should not maintain
parallel compatibility representations.

## Module boundaries

Packages are divided by authority and responsibility, not by file size alone.
Routes register HTTP transport, implementation modules orchestrate request
use-cases, and services own reusable domain behavior and infrastructure
adapters. A split is useful only when the new owner has a clear invariant and a
public operation that callers can depend on.

Dependencies point from transport toward behavior. A service does not acquire
an HTTP dependency merely to reuse a helper, and a route does not bypass its
implementation boundary to assemble domain behavior. Public imports expose the
owning operation; re-export meshes and forwarding shims do not create a second
owner. Current placement exceptions are bounded by the
[import policy](../coding-style.md) and remain explicit findings rather than a
general reverse-dependency allowance.
