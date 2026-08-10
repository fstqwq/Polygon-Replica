# Implementation findings

This ledger contains current technical debt only. `defect` violates a current
contract, `risk` identifies a concrete reliability/security/operations hazard,
and `refactor` identifies misplaced or over-coupled responsibility. Priority is
based on present impact, not on an imagined future architecture.

## Storage and persistence

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| STO-003 | refactor | Schema upgrade handling includes an inline special-case table rebuild in general startup DDL code. | database bootstrap |
| STO-004 | risk | The repository documents storage authorities but has no single consistent multi-root backup/restore implementation. | operations |
| STO-005 | refactor | `workspaces.recent_verification_status` remains in DDL and cleanup SQL even though production status reads and writes no longer use it. | workspace persistence |
| STO-007 | risk | Exclusive cleanup has an audit result but no durable resumable operation record; a process crash can require operator inspection. | maintenance |
| STO-008 | risk | Workspace archive application replaces allowed roots in place and keeps its rollback copy only in a temporary directory; interruption during copying can leave a partial checkout. | workspace archive |
| STO-009 | refactor | Runtime artifact ownership is reconstructed across JSON results and reference tables rather than one queryable owner index. | execution storage |

## Problem source and execution

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| SRCFMT-001 | refactor | Authored configuration readers accept several loose shapes, increasing normalization paths and review cost. | problem source |
| SRCFMT-002 | refactor | Solution metadata still accepts inferred/unkeyed forms instead of one explicit canonical input shape. | problem source |
| SRCFMT-004 | risk | When a checker, validator, or interactor is not configured and several eligible files exist, component selection silently uses the lexicographically first file. | problem build configuration |
| EXE-001 | refactor | Some cancellation outcomes are represented as failed status plus reason text, complicating status queries. | verification |
| EXE-002 | refactor | Admission and execution use overlapping `running` language even though queued callable state and domain lifecycle differ. | worker/runtime |

## Judgehost

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| JH-005 | refactor | Compile failure and host infrastructure failure share parts of the result path and are not uniformly classified. | result processing |

## Placement and maintainability

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| PLC-001 | refactor | Runtime composition is located below an HTTP/auth implementation package. | runtime |
| PLC-002 | refactor | Some verification locator resolution is performed by workspace implementation code. | verification storage |
| PLC-003 | refactor | Several HTTP implementation modules construct large read models and contain domain aggregation. | domain services |
| PLC-004 | refactor | The canonical execution result model is nested under verification although Judgehost and custom run also consume it. | execution model |
| PLC-005 | refactor | Verification coordination remains partly in workspace/request implementation. | verification |
| PLC-006 | refactor | Terminal-result publication now crosses a narrow verification completion sink, but Judgehost composition and scheduling still depend directly on verification persistence, identity, result, and scheduler modules. | verification port |
| PLC-007 | refactor | `app/service/judgehost/result.py` combines callback validation, artifact handling, verdict mapping, telemetry, and publication. | Judgehost result |
| PLC-008 | refactor | Significant contest build policy remains in `app/impl/contest/shared.py`. | contest service |
| PLC-009 | refactor | Filesystem storage concerns are split across several service packages without one locator boundary. | disk/platform |
| PLC-010 | refactor | Maintenance mechanics and domain deletion policy are implemented together. | platform maintenance |
| PLC-011 | refactor | Top-level application startup imports a private authentication implementation helper. | runtime |
| PLC-012 | refactor | Cross-resource authorization policy has no single service owner. | auth/access |
| PLC-014 | refactor | Audit write policy is coupled to workspace and maintenance services. | audit service |

## Operations

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| OPS-003 | risk | Docker build steps use broad failure suppression around some package/TeX setup, which can defer dependency failure until runtime. | container build |
| OPS-004 | risk | The host installer replaces `/etc/polygon-replica.env` with mode `0644`; a rerun drops an operator-added encryption key, while adding the key without tightening permissions exposes it to local users. | host installer |

## Testing and CI

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| TST-001 | refactor | UI and public-contract tests frequently pin template text, CSS classes, DOM fragments, and internal read-model details, so presentation changes accumulate assertions without adding equivalent behavioral protection. | test suite |

## Resolved in this rewrite

- JH-003: invalid hostnames are rejected instead of aliased.
- JH-004: successful and idempotent final callbacks return JSON `1`.
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
- SRC-001 was removed: `app/` is the current package tree, not a deviation from
  an unimplemented future layout.
