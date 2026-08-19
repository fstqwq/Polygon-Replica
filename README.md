# Polygon Replica

Polygon Replica is a self-hosted problem-setting system compatible with Codeforces Polygon. It uses [DOMjudge](https://www.domjudge.org/) Judgehosts for execution, with the browser as its primary interface.

- **Better compatibility.** Existing Polygon sources and working habits carry over directly. Interactive and multi-pass problems are native, first-class problem types. Their complete pass structure is visible in the UI and rendered faithfully in TeX and HTML samples. The same workflow produces packages that can be delivered directly to multiple contest systems.
- **Better scalability.** DOMjudge Judgehosts scale execution independently across remote machines.
- **Better AI integration.** Agents work through the same permission model as human users. [Polygon-Skills](https://github.com/fstqwq/Polygon-Skills) gives them the project context and conventions needed to apply good taste.

## Installation

Polygon Replica runs on Linux. Follow the [deployment runbook](docs/operations/deployment.md) for Docker Compose and systemd installations, sandbox prerequisites, Judgehost setup, TLS, upgrades, backups, and recovery.

## Documentation

- [Documentation index](docs/README.md)
- [Product scope and rationale](docs/product.md)
- [System design](docs/design/system.md)
- [Problem source format](docs/protocol/problem-source.md)
- [Execution and verification](docs/protocol/execution.md)
- [Package import and export](docs/protocol/package.md)
- [Access model](docs/design/access.md)
- [Storage and cleanup](docs/protocol/storage.md)
- [Configuration](docs/operations/configuration.md)
- [Testing policy](docs/testing.md)

The [package protocol](docs/protocol/package.md) lists supported import and export formats, the [execution protocol](docs/protocol/execution.md) explains verification, and the [storage protocol](docs/protocol/storage.md) describes the source, derived, and cache data classes.
