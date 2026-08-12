# Implementation findings

This ledger contains current technical debt only. `defect` violates a current
contract, `risk` identifies a concrete reliability/security/operations hazard,
and `refactor` identifies misplaced or over-coupled responsibility. Priority is
based on present impact, not on an imagined future architecture.

## Storage and persistence

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| STO-009 | refactor | Runtime artifact ownership is reconstructed across JSON results and reference tables rather than one queryable owner index. | execution storage |

## Problem source and execution

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| SRCFMT-001 | refactor | Authored configuration readers accept several loose shapes, increasing normalization paths and review cost. | problem source |
| SRCFMT-002 | refactor | Solution metadata still accepts inferred/unkeyed forms instead of one explicit canonical input shape. | problem source |
| EXE-001 | refactor | Some cancellation outcomes are represented as failed status plus reason text, complicating status queries. | verification |
| EXE-002 | refactor | Admission and execution use overlapping `running` language even though queued callable state and domain lifecycle differ. | worker/runtime |

## Placement and maintainability

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| PLC-001 | refactor | Process lifecycle has a dedicated runtime owner, but the process-wide `RuntimeConfig` composition root remains under `app.impl` and directly aggregates service construction and storage roots. | runtime |
| PLC-002 | refactor | Some verification locator resolution is performed by workspace implementation code. | verification storage |
| PLC-003 | refactor | Several HTTP implementation modules construct large read models and contain domain aggregation. | domain services |
| PLC-004 | refactor | The canonical execution result model is nested under verification although Judgehost and custom run also consume it. | execution model |
| PLC-006 | refactor | Judgehost dependency inversion is implemented on the refactor branch but remains open until Linux Judgehost service and mock-wire acceptance complete. | verification port |
| PLC-008 | refactor | Significant contest build policy remains in `app/impl/contest/shared.py`. | contest service |
| PLC-009 | refactor | Filesystem storage concerns are split across several service packages without one locator boundary. | disk/platform |
| PLC-010 | refactor | Maintenance mechanics and domain deletion policy are implemented together. | platform maintenance |
| PLC-012 | refactor | Cross-resource authorization policy has no single service owner. | auth/access |
| PLC-014 | refactor | Audit write policy is coupled to workspace and maintenance services. | audit service |
