# `app/service/run`

Provides the custom-run test-name rule, re-exports canonical run-limit helpers
from verification, and normalizes/truncates diagnostics for stored summaries.
It owns no database or filesystem state and does not schedule work.

Request preparation and presentation remain in `app/impl`; execution is stored
as a verification with the custom kind and uses the same task DAG, Judgehost
cases, results, and cache payloads. That lifecycle and its persistence are owned by
the [execution protocol](../../../../protocol/execution.md).
