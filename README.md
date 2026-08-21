# Polygon Replica

Polygon Replica is a self-hosted problem-setting system compatible with Codeforces Polygon. It uses [DOMjudge](https://www.domjudge.org/) Judgehosts for execution, with the browser as its primary interface.

- **Better compatibility.** Existing Polygon sources and working habits carry over directly. Interactive and multi-pass problems are native, first-class problem types. Their complete pass structure is visible in the UI and rendered faithfully in TeX and HTML samples. The same workflow produces packages that can be delivered directly to multiple contest systems.
- **Better scalability.** DOMjudge Judgehosts scale execution independently across remote machines. Use `domjudge/judgehost:latest` directly, or use the [modified version for long-running stability](docs/operations/deployment.md#judgehost-image-choice). The system is designed around content-addressed caching, reducing repeated computation and deduplicating stored data as workloads grow.
- **Better AI integration.** Agents work through the same permission model as human users. [Polygon-Skills](https://github.com/fstqwq/Polygon-Skills) gives them the project context and conventions needed to apply good taste.

## Installation

Polygon Replica runs on Linux. Follow the [deployment runbook](docs/operations/deployment.md) for Docker Compose and systemd installations, sandbox prerequisites, Judgehost setup, TLS, upgrades, backups, and recovery.

## Documentation

- [Deployment and operations](docs/operations/deployment.md)
- [Problem source format](docs/protocol/problem-source.md)
- [Package import and export](docs/protocol/package.md)
- [Development documentation index](docs/README.md)
