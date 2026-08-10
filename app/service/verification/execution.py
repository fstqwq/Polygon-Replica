from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypedDict

from app.service.verification.completion import VerificationTaskCompletionService
from app.service.verification.lifecycle import (
    VerificationSnapshot,
    VerificationTransitionCommit,
)
from app.service.verification.runtime_registry import (
    VerificationRuntimeAlreadyRegistered,
    VerificationRuntimeRegistry,
)
from app.service.verification.task_scheduler import (
    TaskPublishResult,
    VerificationRuntimeCallbacks,
    VerificationRuntimeCoordinator,
)
from app.service.verification.task_store import VerificationTaskRow, VerificationTaskStore
from app.service.verification.types import Status


class VerificationDrainSummary(TypedDict):
    cancelled_cases: int
    awaiting_receipts: int
    affected_tasks: int
    affected_batches: int


class VerificationLifecycle(Protocol):
    def verification_snapshot(
        self,
        verification_id: str,
    ) -> VerificationSnapshot | None: ...

    def fail_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationTransitionCommit: ...

    def cancel_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationTransitionCommit: ...


class VerificationExecutionDrainer(Protocol):
    def request_verification_cancel(
        self,
        verification_id: str,
        reason: str,
    ) -> dict[str, int]: ...


@dataclass(frozen=True)
class VerificationExecutionCallbacks:
    publish_task: Callable[[VerificationTaskRow], TaskPublishResult]
    probe_task_case_cache: Callable[[list[str]], set[str]]
    close_programs: Callable[[list[str]], None]
    reconcile_expired_leases: Callable[[], list[str]] = lambda: []


@dataclass(frozen=True)
class VerificationExecutionTransition:
    transition: VerificationTransitionCommit
    drain: VerificationDrainSummary


class VerificationCoordinatorFailure(RuntimeError):
    pass


class VerificationRuntimeDrainFailure(RuntimeError):
    def __init__(self, reason: str, cause: Exception) -> None:
        self.reason = reason
        super().__init__(f"Judgehost drain failed: {cause}")


logger = logging.getLogger(__name__)


def _empty_drain_summary() -> VerificationDrainSummary:
    return {
        "cancelled_cases": 0,
        "awaiting_receipts": 0,
        "affected_tasks": 0,
        "affected_batches": 0,
    }


class VerificationExecutionService:
    def __init__(
        self,
        lifecycle: VerificationLifecycle,
        task_store: VerificationTaskStore,
        completion_service: VerificationTaskCompletionService,
        registry: VerificationRuntimeRegistry,
        drainer: VerificationExecutionDrainer,
    ) -> None:
        self._lifecycle = lifecycle
        self._task_store = task_store
        self._completion_service = completion_service
        self._registry = registry
        self._drainer = drainer

    def _drain(self, verification_id: str, reason: str) -> VerificationDrainSummary:
        result = self._drainer.request_verification_cancel(
            verification_id,
            reason,
        )
        return {
            "cancelled_cases": int(result["cancelled_cases"]),
            "awaiting_receipts": int(result["awaiting_receipts"]),
            "affected_tasks": int(result["affected_tasks"]),
            "affected_batches": int(result["affected_batches"]),
        }

    def _drain_with_retry(
        self,
        verification_id: str,
        reason: str,
    ) -> VerificationDrainSummary:
        try:
            return self._drain(verification_id, reason)
        except Exception:
            logger.warning(
                "retrying Judgehost drain verification_id=%s",
                verification_id,
                exc_info=True,
            )
            return self._drain(verification_id, reason)

    def _transition_runtime(
        self,
        transition: VerificationTransitionCommit,
        *,
        reason: str,
        drain_closed: bool = False,
    ) -> VerificationExecutionTransition:
        if transition.outcome == "missing" or (
            transition.outcome == "closed" and not drain_closed
        ):
            return VerificationExecutionTransition(
                transition=transition,
                drain=_empty_drain_summary(),
            )
        event_error: Exception | None = None
        if transition.outcome == "transitioned":
            try:
                self._registry.cancelled(transition.verification_id, reason)
            except Exception as exc:
                try:
                    self._registry.closed(transition.verification_id)
                except Exception as closed_exc:
                    event_error = RuntimeError(
                        "verification runtime cancellation and closed-event "
                        f"delivery failed: {exc}; {closed_exc}"
                    )
                else:
                    logger.warning(
                        "verification runtime cancellation event failed; "
                        "closed event delivered verification_id=%s",
                        transition.verification_id,
                        exc_info=True,
                    )
        else:
            try:
                self._registry.closed(transition.verification_id)
            except Exception as exc:
                event_error = exc
        try:
            drain = self._drain_with_retry(
                transition.verification_id,
                reason,
            )
        except Exception as drain_error:
            if event_error is not None:
                raise RuntimeError(
                    "verification runtime event delivery failed and "
                    f"Judgehost drain failed: {event_error}; {drain_error}"
                ) from event_error
            raise
        if event_error is not None:
            raise event_error
        return VerificationExecutionTransition(
            transition=transition,
            drain=drain,
        )

    def cancel_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationExecutionTransition:
        transition = self._lifecycle.cancel_verification(
            verification_id,
            reason=reason,
        )
        return self._transition_runtime(
            transition,
            reason=reason,
            drain_closed=True,
        )

    def fail_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationExecutionTransition:
        transition = self._lifecycle.fail_verification(
            verification_id,
            reason=reason,
        )
        return self._transition_runtime(
            transition,
            reason=reason,
            drain_closed=True,
        )

    def run(
        self,
        verification_id: str,
        *,
        callbacks: VerificationExecutionCallbacks,
        edges: list[tuple[str, str]],
    ) -> None:
        def _cancel_execution(reason: str) -> None:
            try:
                self._drain(verification_id, reason)
            except Exception as exc:
                raise VerificationRuntimeDrainFailure(reason, exc) from exc

        runtime_callbacks = VerificationRuntimeCallbacks(
            publish_task=callbacks.publish_task,
            probe_task_case_cache=callbacks.probe_task_case_cache,
            cancel_execution=_cancel_execution,
            close_programs=callbacks.close_programs,
            reconcile_expired_leases=callbacks.reconcile_expired_leases,
        )
        try:
            coordinator = VerificationRuntimeCoordinator(
                verification_id,
                task_store=self._task_store,
                completion_service=self._completion_service,
                callbacks=runtime_callbacks,
                edges=edges,
            )
        except Exception as exc:
            reason = str(exc) or "verification scheduler failed"
            try:
                self.fail_verification(verification_id, reason=reason)
            except Exception as cleanup_exc:
                raise VerificationCoordinatorFailure(
                    f"{reason}; runtime cleanup failed: {cleanup_exc}"
                ) from exc
            raise VerificationCoordinatorFailure(reason) from exc
        try:
            self._registry.register(verification_id, coordinator)
        except VerificationRuntimeAlreadyRegistered as exc:
            raise VerificationCoordinatorFailure(str(exc)) from exc
        try:
            snapshot = self._lifecycle.verification_snapshot(verification_id)
            if snapshot is None:
                raise RuntimeError("verification disappeared before execution")
            if str(snapshot["record"]["status"]) != Status.RUNNING.value:
                coordinator.enqueue_closed()
            coordinator.run()
        except Exception as exc:
            reason = (
                exc.reason
                if isinstance(exc, VerificationRuntimeDrainFailure)
                else str(exc) or "verification scheduler failed"
            )
            try:
                transition = self._lifecycle.fail_verification(
                    verification_id,
                    reason=reason,
                )
                self._transition_runtime(
                    transition,
                    reason=reason,
                    drain_closed=True,
                )
            except Exception as cleanup_exc:
                raise VerificationCoordinatorFailure(
                    f"{reason}; runtime cleanup failed: {cleanup_exc}"
                ) from exc
            raise VerificationCoordinatorFailure(reason) from exc
        finally:
            primary_exception_active = sys.exc_info()[0] is not None
            try:
                unregistered = self._registry.unregister(
                    verification_id,
                    coordinator,
                )
            except Exception:
                if not primary_exception_active:
                    raise
                logger.exception(
                    "failed to unregister verification runtime: %s",
                    verification_id,
                )
                unregistered = True
            if not unregistered:
                message = (
                    "verification runtime ownership changed: "
                    f"{verification_id}"
                )
                if not primary_exception_active:
                    raise RuntimeError(message)
                logger.error(message)
