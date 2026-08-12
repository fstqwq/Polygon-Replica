# Documentation

Read the smallest set that owns the change.

## System

- [Design principles](design/principles.md)
- [Runtime and component model](design/system.md)
- [Access model](design/access.md)

## Protocols

- [Protocol ownership index](protocol/README.md)
- [Problem source](protocol/problem-source.md)
- [Execution and verification](protocol/execution.md)
- [Judgehost wire protocol](protocol/judgehost.md)
- [Storage roots and cleanup](protocol/storage.md)
- [SQLite persistence](protocol/persistence.md)
- [Package import and export](protocol/package.md)

## Implementation and operations

- [Application package map](src/README.md)
- [Python coding and import policy](coding-style.md)
- [Testing policy](testing.md)
- [SQLite implementation notes](implementation/sqlite.md)
- [Findings ledger](implementation/findings.md)
- [Configuration](operations/configuration.md)
- [Runtime and deployment](operations/runtime.md)
- [Operator deployment runbook](operations/deployment.md)

The protocol documents are the source of truth for current exchanged and
persisted shapes. The package map explains where those contracts are
implemented. The findings ledger is not a second design specification.
