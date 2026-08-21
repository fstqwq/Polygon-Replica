import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

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
from app.service.verification.types import VerificationStatus


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
    ) -> None: ...


@dataclass(frozen=True)
class VerificationExecutionCallbacks:
    publish_task: Callable[[VerificationTaskRow], TaskPublishResult]
    probe_task_case_cache: Callable[[list[str]], set[str]]
    close_programs: Callable[[list[str]], None]
    reconcile_expired_leases: Callable[[], list[str]] = lambda: []


@dataclass(frozen=True)
class VerificationExecutionTransition:
    transition: VerificationTransitionCommit


class VerificationCoordinatorFailure(RuntimeError):
    pass


logger = logging.getLogger(__name__)


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

    def _request_runtime_cancel(self, verification_id: str, reason: str) -> None:
        self._drainer.request_verification_cancel(
            verification_id,
            reason,
        )

    def _transition_runtime(
        self,
        transition: VerificationTransitionCommit,
        *,
        reason: str,
        cancellation: bool,
        drain_closed: bool = False,
    ) -> VerificationExecutionTransition:
        if transition.outcome == "missing" or (
            transition.outcome == "closed" and not drain_closed
        ):
            return VerificationExecutionTransition(transition=transition)
        self._request_runtime_cancel(transition.verification_id, reason)
        if transition.outcome == "transitioned" and cancellation:
            try:
                self._registry.cancelled(transition.verification_id, reason)
            except Exception as exc:
                try:
                    self._registry.closed(transition.verification_id)
                except Exception as closed_exc:
                    logger.error(
                        "verification runtime cancellation and closed-event delivery "
                        "failed verification_id=%s cancellation_error=%s closed_error=%s",
                        transition.verification_id,
                        exc,
                        closed_exc,
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
            except Exception:
                logger.exception(
                    "verification runtime closed-event delivery failed "
                    "verification_id=%s",
                    transition.verification_id,
                )
        return VerificationExecutionTransition(transition=transition)

    def cancel_verification(
        self,
        verification_id: str,
        *,
        reason: str,
    ) -> VerificationExecutionTransition:
        started = time.monotonic()
        transition = self._lifecycle.cancel_verification(
            verification_id,
            reason=reason,
        )
        logger.info(
            "verification durable cancellation committed "
            "verification_id=%s outcome=%s elapsed_sec=%.3f",
            verification_id,
            transition.outcome,
            time.monotonic() - started,
        )
        return self._transition_runtime(
            transition,
            reason=reason,
            cancellation=True,
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
            cancellation=False,
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
            self._request_runtime_cancel(verification_id, reason)

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
            if str(snapshot["record"]["status"]) != VerificationStatus.RUNNING.value:
                coordinator.enqueue_closed()
            coordinator.run()
        except Exception as exc:
            reason = str(exc) or "verification scheduler failed"
            try:
                transition = self._lifecycle.fail_verification(
                    verification_id,
                    reason=reason,
                )
                self._transition_runtime(
                    transition,
                    reason=reason,
                    cancellation=False,
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
