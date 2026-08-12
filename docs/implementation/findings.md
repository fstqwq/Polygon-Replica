# Implementation findings

This ledger contains current technical debt only. `defect` violates a current
contract, `risk` identifies a concrete reliability/security/operations hazard,
and `refactor` identifies misplaced or over-coupled responsibility. Priority is
based on present impact, not on an imagined future architecture.

## Placement and maintainability

| ID | Class | Finding and current impact | Suggested owner |
| --- | --- | --- | --- |
| PLC-003 | refactor | Three HTTP-facing modules still own reusable aggregation: `app/impl/workspace/run_view_detail.py` joins verification, task, artifact, diagnostic, and sanity evidence into the run-detail model; `app/impl/contest/problem_rows.py` joins roster, access, workspace revision, source review, and readiness state; `app/impl/workspace/context_operation.py` joins authored files, solution metadata, test specifications, and verification artifacts into editor/run option models. Their size and direct domain queries make non-HTTP reuse and isolated review difficult. | verification, contest, and problem query services |
