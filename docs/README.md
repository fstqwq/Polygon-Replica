# Documentation

Polygon Replica documentation covers product scope, system design, protocols,
implementation structure, testing, and deployment.

## System

- [Product scope and rationale](product.md)
- [State derivation and lifecycle](design/state-lifecycle.md)
- [Runtime and component model](design/system.md)
- [Access model](design/access.md)

## Protocols

- [Protocol index](protocol/README.md)
- [Problem source](protocol/problem-source.md)
- [Execution and verification](protocol/execution.md)
- [Judgehost wire protocol](protocol/judgehost.md)
- [Storage roots and cleanup](protocol/storage.md)
- [SQLite persistence](protocol/persistence.md)
- [Package import and export](protocol/package.md)
- [Statement Preview](protocol/statement-preview.md)

## Implementation and operations

- [Application package map](src/README.md)
- [Python coding and import policy](coding-style.md)
- [Testing policy](testing.md)
- [SQLite implementation notes](implementation/sqlite.md)
- [Findings ledger](implementation/findings.md)
- [Configuration](operations/configuration.md)
- [Runtime and deployment](operations/runtime.md)
- [Operator deployment runbook](operations/deployment.md)
