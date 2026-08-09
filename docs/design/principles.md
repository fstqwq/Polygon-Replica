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
