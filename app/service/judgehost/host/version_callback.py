"""Own the Judgehost toolchain-version callback handshake."""

from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.callback.model import CallbackOutcome
from app.service.judgehost.host.toolchain_versions import (
    ToolchainTelemetryHandler,
    ToolchainVersionReport,
)
from app.service.judgehost.validation import normalize_judgehost_hostname


class JudgehostVersionCallback:
    def __init__(
        self,
        batch_runtime: JudgehostBatchRuntime,
        telemetry: ToolchainTelemetryHandler,
    ) -> None:
        self._batch_runtime = batch_runtime
        self._telemetry = telemetry

    def commands(self, judgetask_id: int) -> dict[str, object]:
        return self._telemetry.version_commands(int(judgetask_id))

    def report(
        self,
        judgetask_id: int,
        *,
        hostname: str,
        compiler: str = "",
        runner: str = "",
    ) -> CallbackOutcome[dict[str, object]]:
        receipt = self._batch_runtime.acquire_case_callback_receipt(
            int(judgetask_id)
        )
        if receipt is None:
            return CallbackOutcome({}, (), (), ())
        safe_host = normalize_judgehost_hostname(hostname)
        try:
            expected_hostname = receipt.lease_owner or receipt.last_callback_hostname
            if receipt.status not in {
                "leased",
                "reporting",
                "reported",
                "cancelled",
            }:
                raise RuntimeError("judgehost case is not in a version callback state")
            if not expected_hostname or expected_hostname != safe_host:
                return CallbackOutcome(
                    {},
                    (),
                    (receipt.verification_id,),
                    (),
                )
            self._telemetry.record_report(
                ToolchainVersionReport(
                    judgetask_id=int(judgetask_id),
                    hostname=safe_host,
                    compiler=compiler,
                    runner=runner,
                    task_id=receipt.task_id,
                    run_id=receipt.run_id,
                )
            )
            return CallbackOutcome(
                {},
                (),
                (receipt.verification_id,),
                (),
            )
        finally:
            self._batch_runtime.release_case_callback_receipt(receipt.receipt_id)
