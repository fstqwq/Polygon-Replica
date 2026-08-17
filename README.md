# Polygon Replica

Polygon Replica is a self-hosted system for authoring, reviewing, verifying,
and delivering programming-contest problems. It provides everything a team
needs to turn an initial solution and a few tests into a contest-ready problem,
plus an agent interface
([Polygon-Skills](https://github.com/fstqwq/Polygon-Skills)) for automating the
same workflow.

Polygon Replica uses [DOMjudge](https://www.domjudge.org/) Judgehosts as its
execution backend, giving multi-pass and interactive problems the same
first-class verification workflow as conventional batch problems.

## The workflow

```text
create or import
      |
      v
per-user workspace ---- verify and review
      |
      | publish
      v
official problem version ---- full verification ---- Native Package
                                                        |
                         +------------------------------+-------------------+
                         |                 |                    |           |
                         v                 v                    v           v
                direct download     package adapters     Contest PDFs  Contest packages
```

A workspace is a private working copy belonging to one user. Publishing saves
its reviewed changes as the next official version of the problem. Internally,
each version is a Git commit on the problem's `main` branch, so the exact source
used for a package can always be identified. Generated inputs, answers, logs,
PDFs, and archives can be cleaned and rebuilt without deleting that history.

After a full verification succeeds, the system produces a **Native Package**
for that official version. It retains the exact published source together with the
generated test inputs and official answers used by the run. It is directly
downloadable. DOMjudge, ICPC 2025-09, QOJ, and Nowcoder packages are external
packages produced by adapters from the same Native Package, not separate
verification results.

Contest builds consume only Native Packages that already exist. They never
start a problem verification implicitly. A Contest can therefore use the
latest Native Package even when it trails the newest published version, and
its readiness page makes that distinction visible.

## Installation

Polygon Replica runs on Linux. Follow the
[deployment runbook](docs/operations/deployment.md) for Docker Compose and
systemd installations, sandbox prerequisites, Judgehost setup, TLS, upgrades,
backups, and recovery.

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

The [package protocol](docs/protocol/package.md) lists supported import and
export formats, the [execution protocol](docs/protocol/execution.md) explains
verification, and the [storage protocol](docs/protocol/storage.md) describes
the source, derived, and cache data classes.
