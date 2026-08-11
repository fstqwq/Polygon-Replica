# Implementation findings

This ledger contains current technical debt only. `defect` violates a current
contract, `risk` identifies a concrete reliability/security/operations hazard,
and `refactor` identifies misplaced or over-coupled responsibility. Priority is
based on present impact, not on an imagined future architecture.

## Storage and persistence

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| STO-003 | refactor | Schema upgrade handling includes an inline special-case table rebuild in general startup DDL code. | database bootstrap |
| STO-005 | refactor | `workspaces.recent_verification_status` remains in DDL and cleanup SQL even though production status reads and writes no longer use it. | workspace persistence |
| STO-009 | refactor | Runtime artifact ownership is reconstructed across JSON results and reference tables rather than one queryable owner index. | execution storage |

## Problem source and execution

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| SRCFMT-001 | refactor | Authored configuration readers accept several loose shapes, increasing normalization paths and review cost. | problem source |
| SRCFMT-002 | refactor | Solution metadata still accepts inferred/unkeyed forms instead of one explicit canonical input shape. | problem source |
| EXE-001 | refactor | Some cancellation outcomes are represented as failed status plus reason text, complicating status queries. | verification |
| EXE-002 | refactor | Admission and execution use overlapping `running` language even though queued callable state and domain lifecycle differ. | worker/runtime |
| PKG-005 | risk | Export worker dedupe includes the newly allocated job ID, so repeated requests for the same published revision are not coalesced and can consume duplicate queue capacity or produce avoidable concurrent-materialization failures. | export admission |

## Placement and maintainability

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| PLC-001 | refactor | Runtime composition is located below an HTTP/auth implementation package. | runtime |
| PLC-002 | refactor | Some verification locator resolution is performed by workspace implementation code. | verification storage |
| PLC-003 | refactor | Several HTTP implementation modules construct large read models and contain domain aggregation. | domain services |
| PLC-004 | refactor | The canonical execution result model is nested under verification although Judgehost and custom run also consume it. | execution model |
| PLC-006 | refactor | Lease, completion, and diagnostic events now cross narrow injected ports, but Judgehost still depends directly on verification task storage, program identity, and execution-result models. | verification port |
| PLC-007 | refactor | `app/service/judgehost/result.py` still combines callback validation, artifact capture, toolchain telemetry, and debug-payload parsing; verdict normalization, publication, and batch finalization now have separate owners. | Judgehost callback ingestion |
| PLC-008 | refactor | Significant contest build policy remains in `app/impl/contest/shared.py`. | contest service |
| PLC-009 | refactor | Filesystem storage concerns are split across several service packages without one locator boundary. | disk/platform |
| PLC-010 | refactor | Maintenance mechanics and domain deletion policy are implemented together. | platform maintenance |
| PLC-011 | refactor | Top-level application startup imports a private authentication implementation helper. | runtime |
| PLC-012 | refactor | Cross-resource authorization policy has no single service owner. | auth/access |
| PLC-014 | refactor | Audit write policy is coupled to workspace and maintenance services. | audit service |

## Testing and CI

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| TST-001 | refactor | UI and public-contract tests frequently pin template text, CSS classes, DOM fragments, and internal read-model details, so presentation changes accumulate assertions without adding equivalent behavioral protection. | test suite |

## Resolved in this rewrite

- JH-003: invalid hostnames are rejected instead of aliased.
- JH-004: successful and idempotent final callbacks return JSON `1`.
- JH-005: compile failure normalization and host/batch finalization have
  separate owners in the Judgehost result pipeline.
- RUN-001: startup resets both loaded worker history and its JSONL.
- OPS-001: the installer renders the systemd account and repository path.
- OPS-002: the unused secure-cookie environment variable was removed.
- PKG-003: all ZIP member names are canonicalized and duplicate, conflicting,
  absolute, and traversing paths are rejected before importer selection.
- PKG-004: authenticated ZIP imports preflight entry structure and stream only
  selected content under separate compressed, entry, expanded, and metadata
  budgets; Native materialization payloads are skipped.
- PLC-013: the typed configuration registry now owns every admin-editable
  default and validator, while services consume one atomic `ConfigValues`
  snapshot and fixed constants remain separate.
- PLC-005: verification runtime coordination has one service owner and one
  instance-owned runtime registry.
- SRC-001 was removed: `app/` is the current package tree, not a deviation from
  an unimplemented future layout.
