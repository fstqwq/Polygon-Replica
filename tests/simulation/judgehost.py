from __future__ import annotations

import hashlib
import heapq
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from app.service.judgehost.batch_scheduler_models import CompileSubmission, ExecutionBatchSpec
from app.service.judgehost.case_result import build_case_result
from app.service.judgehost.identity import domjudge_submit_id
from app.service.platform.runtime_blob_store import PayloadFile
from tests.simulation.report import evaluate_assertions
from tests.simulation.strategy import (
    create_scheduler,
    observe_simulated_case,
    observe_simulated_compile,
    register_simulated_case,
)
from tests.simulation.workload import DurationRange, Workload


_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)
_TERMINAL_BATCH_STATUSES = {"completed", "failed"}


@dataclass(slots=True)
class _Node:
    node_id: str
    verification_id: str
    logical_run_id: str
    program_key: str
    timing_kind: str
    scheduler_task_kind: str
    service_class: str
    test_index: int
    scope_sequence: int
    compile_key: str
    compile_duration_sec: float
    case_duration_sec: float
    cache_hit: bool
    parents: tuple[str, ...] = ()
    dependencies_remaining: int = 0
    children: list[str] = field(default_factory=list)
    state: str = "planned"
    ready_at: float | None = None
    completed_at: float | None = None
    task_id: str = ""
    run_id: str = ""
    batch_id: int | None = None
    case_id: int | None = None
    leased_at: float | None = None
    compile_started_at: float | None = None
    leased_hostname: str | None = None
    prior_background_batch_id: int | None = None
    resumed_previous_background: bool | None = None


@dataclass(slots=True)
class _Verification:
    verification_id: str
    arrival_sec: float
    foreground: bool = False
    node_ids: list[str] = field(default_factory=list)
    first_progress_sec: float | None = None
    finished_sec: float | None = None
    critical_path_sec: float = 0.0
    remaining_node_count: int = 0


@dataclass(slots=True)
class _Host:
    hostname: str
    enabled: bool = True
    epoch: int = 0
    state: str = "idle"
    state_since: float = 0.0
    state_times: dict[str, float] = field(
        default_factory=lambda: {"idle": 0.0, "compile": 0.0, "execute": 0.0, "disconnected": 0.0}
    )
    local_compile_cache: set[tuple[str, str]] = field(default_factory=set)
    compile_keys_seen: set[tuple[str, str]] = field(default_factory=set)
    last_compile_key: tuple[str, str] | None = None
    program_switch_count: int = 0
    current_batch_id: int | None = None
    current_rows: list[dict[str, object]] = field(default_factory=list)
    current_index: int = 0
    last_background_batch_id: int | None = None


class JudgehostSimulation:
    """Drive the real in-memory Scheduler from a deterministic virtual event loop."""

    def __init__(
        self,
        workload: Workload,
        *,
        trace: bool = False,
        strategy: str = "production",
    ) -> None:
        self.workload = workload
        self.strategy = strategy
        self.scheduler = create_scheduler(strategy, id_base=1_000_000)
        self._rng = random.Random(workload.seed)
        self._trace_enabled = bool(trace)
        self._trace: list[dict[str, object]] = []
        self._events: list[tuple[float, int, str, tuple[object, ...]]] = []
        self._event_sequence = 0
        self._now = 0.0
        self._nodes: dict[str, _Node] = {}
        self._verifications: dict[str, _Verification] = {}
        self._logical_totals: dict[tuple[str, str], int] = {}
        self._logical_completed: dict[tuple[str, str], int] = {}
        self._case_node_ids: dict[int, str] = {}
        self._batch_ids: set[int] = set()
        self._batch_hosts: dict[int, set[str]] = {}
        self._hosts = [_Host(hostname=f"host-{index + 1}") for index in range(workload.host_count)]
        self._waiting_hosts: set[str] = set()
        self._active_case_ids: set[int] = set()
        self._leased_case_ids: set[int] = set()
        self._reported_case_ids: set[int] = set()
        self._duplicate_report_count = 0
        self._ready_to_lease_sec: list[float] = []
        self._invariant_violations: list[str] = []
        self._compile_count = 0
        self._compile_time_sec = 0.0
        self._lease_attempt_count = 0
        self._cache_hit_count = 0
        self._empty_fetch_count = 0
        self._poll_backoff_sec = 0.0
        self._long_poll_wake_count = 0
        self._background_leases_while_foreground_waited = 0
        self._foreground_resume_by_host: dict[str, tuple[str, int | None]] = {}
        self._build_graph()
        self._remaining_node_count = len(self._nodes)

    def run(self) -> dict[str, object]:
        for verification in self._verifications.values():
            self._schedule(
                verification.arrival_sec,
                "verification_arrival",
                verification.verification_id,
            )
        for event in self.workload.host_disconnect_events:
            host = self._hosts[event.host_index]
            self._schedule(event.at_sec, "host_disconnect", host.hostname)
            self._schedule(event.at_sec + event.duration_sec, "host_reconnect", host.hostname)
        for host in self._hosts:
            self._schedule(0.0, "host_fetch", host.hostname, host.epoch)

        max_events = max(100_000, len(self._nodes) * 100 + len(self._hosts) * 10_000)
        handled = 0
        while self._events and handled < max_events:
            at_sec, _sequence, kind, payload = heapq.heappop(self._events)
            self._now = at_sec
            handled += 1
            handler = self._event_handlers().get(kind)
            if handler is None:
                self._violate(f"unknown event kind: {kind}")
                continue
            handler(*payload)
            if self._all_complete() and not self._active_case_ids:
                break
        if handled >= max_events:
            self._violate("simulation event limit exceeded")
        if not self._all_complete():
            self._violate("simulation stopped with unfinished Cases")
        makespan = max(
            (verification.finished_sec or self._now)
            for verification in self._verifications.values()
        )
        for host in self._hosts:
            self._transition_host(host, host.state, at_sec=makespan)
        return self._report(makespan)

    def _event_handlers(self) -> Mapping[str, Callable[..., None]]:
        return {
            "verification_arrival": self._verification_arrival,
            "host_fetch": self._host_fetch,
            "compile_finish": self._compile_finish,
            "case_finish": self._case_finish,
            "host_disconnect": self._host_disconnect,
            "host_reconnect": self._host_reconnect,
        }

    def _build_graph(self) -> None:
        for verification_index in range(self.workload.verification_count):
            verification_id = f"ver-{verification_index + 1:x}"
            verification = _Verification(
                verification_id=verification_id,
                arrival_sec=verification_index * self.workload.verification_arrival_gap_sec,
            )
            self._verifications[verification_id] = verification
            slow_count = int(
                round(self.workload.solution_count * self.workload.slow_solution_fraction)
            )
            slow_programs = set(
                self._rng.sample(
                    range(self.workload.solution_count),
                    min(slow_count, self.workload.solution_count),
                )
            )
            solution_nodes: list[str] = []
            for test_index in range(1, self.workload.tests_per_verification + 1):
                previous: str | None = None
                generated = (
                    self.workload.generator_enabled
                    and test_index > self.workload.manual_test_count
                )
                if generated:
                    previous = self._add_node(
                        verification,
                        timing_kind="generate-input",
                        program_key="generator",
                        test_index=test_index,
                        dependencies=(),
                    )
                if self.workload.main_correct_enabled:
                    previous = self._add_node(
                        verification,
                        timing_kind="main-correct",
                        program_key="main-correct",
                        test_index=test_index,
                        dependencies=() if previous is None else (previous,),
                    )
                for solution_index in range(self.workload.solution_count):
                    multiplier = self.workload.slow_solution_multiplier
                    if solution_index not in slow_programs:
                        multiplier = 1.0
                    node_id = self._add_node(
                        verification,
                        timing_kind="solution-run",
                        program_key=f"solution-{solution_index + 1}",
                        test_index=test_index,
                        dependencies=() if previous is None else (previous,),
                        duration_multiplier=multiplier,
                    )
                    solution_nodes.append(node_id)
            for sanity_index in range(self.workload.sanity_probe_count):
                self._add_node(
                    verification,
                    timing_kind="sanity",
                    program_key=f"sanity-{sanity_index + 1}",
                    test_index=self.workload.tests_per_verification + sanity_index + 1,
                    dependencies=tuple(solution_nodes),
                )
            verification.critical_path_sec = self._critical_path(verification.node_ids)
            verification.remaining_node_count = len(verification.node_ids)
        for foreground_index, task in enumerate(self.workload.foreground_tasks, start=1):
            verification = _Verification(
                verification_id=f"ver-f{foreground_index:08x}",
                arrival_sec=task.arrival_sec,
                foreground=True,
            )
            self._verifications[verification.verification_id] = verification
            self._add_node(
                verification,
                timing_kind="foreground",
                program_key=f"foreground-compile-{foreground_index}",
                test_index=1,
                dependencies=(),
                compile_duration_sec=task.compile_time_sec,
            )
            verification.critical_path_sec = self._critical_path(verification.node_ids)
            verification.remaining_node_count = len(verification.node_ids)

    def _add_node(
        self,
        verification: _Verification,
        *,
        timing_kind: str,
        program_key: str,
        test_index: int,
        dependencies: tuple[str, ...],
        duration_multiplier: float = 1.0,
        compile_duration_sec: float | None = None,
    ) -> str:
        sequence = len(verification.node_ids) + 1
        node_id = f"{verification.verification_id}:{program_key}:{test_index}"
        if node_id in self._nodes:
            raise ValueError(f"duplicate simulation node: {node_id}")
        compile_key = _sha256(f"compile:{self.workload.name}:{program_key}")
        duration_range = self.workload.case_time_distribution_by_kind[timing_kind]
        duration = self._duration(duration_range) * duration_multiplier
        scheduler_kind = (
            "compile-only"
            if timing_kind == "foreground"
            else timing_kind
            if timing_kind in {"generate-input", "main-correct"}
            else "solution-run"
        )
        service_class = "foreground" if timing_kind == "foreground" else "background"
        node = _Node(
            node_id=node_id,
            verification_id=verification.verification_id,
            logical_run_id=f"logical:{program_key}",
            program_key=program_key,
            timing_kind=timing_kind,
            scheduler_task_kind=scheduler_kind,
            service_class=service_class,
            test_index=test_index,
            scope_sequence=sequence,
            compile_key=compile_key,
            compile_duration_sec=(
                float(self.workload.compile_time_sec_by_kind[timing_kind])
                if compile_duration_sec is None
                else float(compile_duration_sec)
            ),
            case_duration_sec=duration,
            cache_hit=self._rng.random() < self.workload.cache_hit_rate,
            parents=dependencies,
            dependencies_remaining=len(dependencies),
            task_id=f"task:{node_id}",
            run_id=f"run:{node_id}",
        )
        self._nodes[node_id] = node
        verification.node_ids.append(node_id)
        logical_key = (node.verification_id, node.logical_run_id)
        self._logical_totals[logical_key] = self._logical_totals.get(logical_key, 0) + 1
        for dependency_id in dependencies:
            self._nodes[dependency_id].children.append(node_id)
        return node_id

    def _critical_path(self, node_ids: list[str]) -> float:
        totals: dict[str, float] = {}
        for node_id in node_ids:
            node = self._nodes[node_id]
            parent_total = max((totals[parent] for parent in node.parents), default=0.0)
            totals[node_id] = parent_total + node.case_duration_sec + node.compile_duration_sec
        return max(totals.values(), default=0.0)

    def _verification_arrival(self, verification_id: object) -> None:
        verification = self._verifications[str(verification_id)]
        self._trace_event(
            "foreground_arrival" if verification.foreground else "verification_arrival",
            verification_id=verification.verification_id,
        )
        roots = [
            self._nodes[node_id]
            for node_id in verification.node_ids
            if self._nodes[node_id].dependencies_remaining == 0
        ]
        for node in roots:
            self._release_node(node)

    def _release_node(self, node: _Node) -> None:
        if node.state != "planned" or node.dependencies_remaining != 0:
            self._violate(f"invalid ready transition for {node.node_id}")
            return
        node.state = "ready"
        node.ready_at = self._now
        self._trace_event("task_ready", node=node.node_id)
        batch_id = self.scheduler.create_batch_with_cases(
            task_id=node.task_id,
            run_id=node.run_id,
            logical_run_id=node.logical_run_id,
            execution_signature=_sha256(f"execution:{node.program_key}"),
            task_kind=node.scheduler_task_kind,
            verification_id=node.verification_id,
            compile_key=node.compile_key,
            compile_submission=self._compile_submission(node),
            contest_id="simulation",
            mode="pass-fail",
            source_name=f"{node.program_key}.cpp",
            compile_hash=_sha256(f"compile-script:{node.program_key}")[:32],
            run_hash=_sha256(f"run-script:{node.program_key}")[:32],
            compare_hash=_sha256(f"compare-script:{node.program_key}")[:32],
            source_hash=node.compile_key,
            compile_config_json="{}",
            run_config_json="{}",
            compare_config_json="{}",
            expected_behavior="accepted",
            verification_source=node.timing_kind,
            bypass_case_result_cache=0,
            service_class=node.service_class,
            batch_spec=ExecutionBatchSpec(),
            created_at=self._now_text(),
            case_rows=[self._case_row(node)],
        )
        node.batch_id = batch_id
        self._batch_ids.add(batch_id)
        self._batch_hosts.setdefault(batch_id, set())
        if not self.scheduler.activate_task_cases(node.task_id, now_text=self._now_text()):
            self._violate(f"failed to activate {node.node_id}")
            return
        claims = self.scheduler.claim_cache_cases(
            batch_id,
            hostname="simulation-cache",
            limit=1,
            now_text=self._now_text(),
        )
        if len(claims) != 1 or claims[0][0].task_id != node.task_id:
            self._violate(f"unexpected cache claim for {node.node_id}")
            return
        claim, row = claims[0]
        node.case_id = int(row["id"])
        self._case_node_ids[node.case_id] = node.node_id
        register_simulated_case(
            self.scheduler,
            batch_id=batch_id,
            case_id=node.case_id,
            case_duration_sec=node.case_duration_sec,
            compile_duration_sec=node.compile_duration_sec,
        )
        if node.cache_hit:
            outcome = self.scheduler.commit_case_result(
                claim.case_id,
                generation=claim.generation,
                result=self._case_result(node),
                updated_at=self._now_text(),
            )
            if outcome != "reported":
                self._violate(f"cache result was not committed for {node.node_id}")
                return
            self._cache_hit_count += 1
            self._reported_case_ids.add(claim.case_id)
            self._trace_event("cache_hit", node=node.node_id, case_id=claim.case_id)
            self._complete_node(node)
        else:
            if not self.scheduler.finish_cache_miss(
                claim.case_id,
                generation=claim.generation,
                updated_at=self._now_text(),
            ):
                self._violate(f"cache miss was not committed for {node.node_id}")
                return
            self._wake_waiting_hosts()

    def _host_fetch(self, hostname: object, epoch: object) -> None:
        host = self._host(str(hostname))
        if not host.enabled or host.epoch != int(epoch) or host.state != "idle":
            return
        self._trace_event("fetch", host=host.hostname)
        batch = self.scheduler.select_ready_batch(host.hostname)
        if batch is None:
            self._empty_fetch_count += 1
            if not self._all_complete():
                if self.workload.long_poll_enabled:
                    self._waiting_hosts.add(host.hostname)
                else:
                    self._poll_backoff_sec += self.workload.fetch_idle_backoff_sec
                    self._schedule(
                        self._now + self.workload.fetch_idle_backoff_sec,
                        "host_fetch",
                        host.hostname,
                        host.epoch,
                    )
            return
        batch_id = int(batch["batch_id"])
        batch_is_foreground = str(batch["service_class"]) == "foreground"
        self._trace_event("batch_selected", host=host.hostname, batch_id=batch_id)
        if str(batch["materialization_state"]) != "ready":
            if self.scheduler.claim_materialization(batch_id, now_text=self._now_text()):
                self.scheduler.finish_materialization(
                    batch_id,
                    success=True,
                    error_text="",
                    now_text=self._now_text(),
                )
        rows = self.scheduler.lease_cases(
            batch_id,
            hostname=host.hostname,
            limit=self.workload.fetch_batch_size,
            now_text=self._now_text(),
        )
        if not rows:
            self._schedule(self._now, "host_fetch", host.hostname, host.epoch)
            return
        if self._foreground_waiting() and not batch_is_foreground:
            self._background_leases_while_foreground_waited += len(rows)
        host.current_batch_id = batch_id
        host.current_rows = [dict(row) for row in rows]
        host.current_index = 0
        self._batch_hosts[batch_id].add(host.hostname)
        compile_key = (str(batch["verification_id"]), str(batch["compile_key"]))
        if host.last_compile_key is not None and host.last_compile_key != compile_key:
            host.program_switch_count += 1
        host.last_compile_key = compile_key
        if batch_is_foreground:
            foreground_node = self._nodes[self._case_node_ids[int(rows[0]["id"])]]
            foreground_node.leased_at = self._now
            foreground_node.leased_hostname = host.hostname
            foreground_node.prior_background_batch_id = host.last_background_batch_id
        else:
            resume = self._foreground_resume_by_host.pop(host.hostname, None)
            if resume is not None:
                node_id, previous_batch_id = resume
                self._nodes[node_id].resumed_previous_background = (
                    previous_batch_id is not None and previous_batch_id == batch_id
                )
            host.last_background_batch_id = batch_id
        for row in rows:
            case_id = int(row["id"])
            if case_id in self._active_case_ids:
                self._violate(f"Case {case_id} was leased while already active")
            self._active_case_ids.add(case_id)
            self._leased_case_ids.add(case_id)
            self._lease_attempt_count += 1
            node = self._nodes[self._case_node_ids[case_id]]
            if node.ready_at is None:
                self._violate(f"Case {case_id} leased before ready")
            else:
                self._ready_to_lease_sec.append(max(0.0, self._now - node.ready_at))
        self._trace_event(
            "cases_leased",
            host=host.hostname,
            batch_id=batch_id,
            case_ids=[int(row["id"]) for row in rows],
            nodes=[self._case_node_ids[int(row["id"])] for row in rows],
        )
        if compile_key in host.local_compile_cache:
            if batch_is_foreground:
                foreground_node.compile_started_at = self._now
            self._transition_host(host, "execute")
            self._schedule_current_case(host)
            return
        host.compile_keys_seen.add(compile_key)
        self._compile_count += 1
        node = self._nodes[self._case_node_ids[int(rows[0]["id"])]]
        if batch_is_foreground:
            node.compile_started_at = self._now
        self._compile_time_sec += node.compile_duration_sec
        self._transition_host(host, "compile")
        self._trace_event("compile_start", host=host.hostname, batch_id=batch_id)
        self._schedule(
            self._now + node.compile_duration_sec,
            "compile_finish",
            host.hostname,
            host.epoch,
            batch_id,
            compile_key,
        )

    def _compile_finish(
        self,
        hostname: object,
        epoch: object,
        batch_id: object,
        compile_key: object,
    ) -> None:
        host = self._host(str(hostname))
        if not host.enabled or host.epoch != int(epoch) or host.current_batch_id != int(batch_id):
            return
        key = tuple(compile_key) if isinstance(compile_key, tuple) else None
        if key is None or len(key) != 2:
            self._violate(f"invalid compile key event for {host.hostname}")
            return
        host.local_compile_cache.add((str(key[0]), str(key[1])))
        observe_simulated_compile(
            self.scheduler,
            batch_id=int(batch_id),
            hostname=host.hostname,
            duration_sec=self._nodes[
                self._case_node_ids[int(host.current_rows[0]["id"])]
            ].compile_duration_sec,
        )
        if not self.scheduler.record_compile_result(
            int(batch_id),
            compile_success=1,
            compile_output_b64="",
            compile_metadata_b64="",
            updated_at=self._now_text(),
        ):
            self._violate(f"compile result rejected for Batch {batch_id}")
        self._trace_event("compile_finish", host=host.hostname, batch_id=int(batch_id))
        self._wake_waiting_hosts()
        self._transition_host(host, "execute")
        self._schedule_current_case(host)

    def _schedule_current_case(self, host: _Host) -> None:
        if host.current_index >= len(host.current_rows):
            self._finish_host_fetch_batch(host)
            return
        row = host.current_rows[host.current_index]
        node = self._nodes[self._case_node_ids[int(row["id"])]]
        self._schedule(
            self._now + node.case_duration_sec,
            "case_finish",
            host.hostname,
            host.epoch,
            int(row["id"]),
        )

    def _case_finish(self, hostname: object, epoch: object, case_id: object) -> None:
        host = self._host(str(hostname))
        numeric_case_id = int(case_id)
        if (
            not host.enabled
            or host.epoch != int(epoch)
            or numeric_case_id not in self._active_case_ids
        ):
            return
        node = self._nodes[self._case_node_ids[numeric_case_id]]
        claim = self.scheduler.claim_case_reporting(
            numeric_case_id,
            hostname=host.hostname,
            now_text=self._now_text(),
        )
        if claim is None:
            self._violate(f"Case report claim rejected for {numeric_case_id}")
            return
        self.scheduler.observe_compile_success_from_case_claim(
            numeric_case_id,
            generation=claim.generation,
            lease_owner=host.hostname,
            updated_at=self._now_text(),
        )
        outcome = self.scheduler.commit_case_result(
            numeric_case_id,
            generation=claim.generation,
            result=self._case_result(node),
            updated_at=self._now_text(),
        )
        if outcome != "reported":
            self._violate(f"Case result rejected for {numeric_case_id}")
            return
        if node.batch_id is None:
            self._violate(f"Case {numeric_case_id} has no Batch identity")
            return
        observe_simulated_case(
            self.scheduler,
            batch_id=node.batch_id,
            hostname=host.hostname,
            duration_sec=node.case_duration_sec,
        )
        self._active_case_ids.discard(numeric_case_id)
        if numeric_case_id in self._reported_case_ids:
            self._duplicate_report_count += 1
            self._violate(f"Case {numeric_case_id} reported more than once")
        self._reported_case_ids.add(numeric_case_id)
        self._trace_event(
            "case_reported",
            host=host.hostname,
            case_id=numeric_case_id,
            node=node.node_id,
        )
        self._complete_node(node)
        self._wake_waiting_hosts()
        host.current_index += 1
        self._schedule_current_case(host)

    def _finish_host_fetch_batch(self, host: _Host) -> None:
        host.current_rows.clear()
        host.current_index = 0
        host.current_batch_id = None
        self._transition_host(host, "idle")
        if not self._all_complete():
            self._schedule(self._now, "host_fetch", host.hostname, host.epoch)

    def _complete_node(self, node: _Node) -> None:
        if node.state == "completed":
            self._violate(f"node completed twice: {node.node_id}")
            return
        node.state = "completed"
        node.completed_at = self._now
        if node.timing_kind == "foreground" and node.leased_hostname is not None:
            self._foreground_resume_by_host[node.leased_hostname] = (
                node.node_id,
                node.prior_background_batch_id,
            )
        self._remaining_node_count -= 1
        verification = self._verifications[node.verification_id]
        verification.remaining_node_count -= 1
        if verification.first_progress_sec is None:
            verification.first_progress_sec = self._now
        logical_key = (node.verification_id, node.logical_run_id)
        self._logical_completed[logical_key] = self._logical_completed.get(logical_key, 0) + 1
        if self._logical_completed[logical_key] == self._logical_totals[logical_key]:
            ready = self.scheduler.finish_logical_runs(
                node.verification_id,
                [node.logical_run_id],
                now_text=self._now_text(),
            )
            self._trace_event(
                "logical_run_closed",
                verification_id=node.verification_id,
                logical_run_id=node.logical_run_id,
            )
            for batch_id in ready:
                self._finalize_batch(batch_id)
        for child_id in node.children:
            child = self._nodes[child_id]
            child.dependencies_remaining -= 1
            if child.dependencies_remaining < 0:
                self._violate(f"dependency underflow for {child.node_id}")
            elif child.dependencies_remaining == 0:
                self._release_node(child)
        if verification.remaining_node_count == 0:
            verification.finished_sec = self._now
            ready = self.scheduler.finish_verification_execution(
                verification.verification_id,
                now_text=self._now_text(),
            )
            for batch_id in ready:
                self._finalize_batch(batch_id)
            for batch_id in self._batch_ids:
                batch = self.scheduler.fetch_batch(batch_id)
                if (
                    batch is not None
                    and str(batch["verification_id"]) == verification.verification_id
                ):
                    self._finalize_batch(batch_id)
            self._trace_event("verification_finished", verification_id=verification.verification_id)

    def _finalize_batch(self, batch_id: int) -> None:
        batch = self.scheduler.fetch_batch(batch_id)
        if batch is None or str(batch["status"]) in _TERMINAL_BATCH_STATUSES:
            return
        claim = self.scheduler.claim_batch_finalization(batch_id, now_text=self._now_text())
        if claim is None:
            return
        if not self.scheduler.set_batch_terminal_status(
            batch_id,
            status="completed",
            completed_at=self._now_text(),
            updated_at=self._now_text(),
        ):
            self._violate(f"failed to finalize Batch {batch_id}")

    def _host_disconnect(self, hostname: object) -> None:
        host = self._host(str(hostname))
        if not host.enabled:
            return
        host.enabled = False
        host.epoch += 1
        self._waiting_hosts.discard(host.hostname)
        released_ids = [int(row["id"]) for row in host.current_rows]
        self.scheduler.release_host_leases(host.hostname, now_text=self._now_text())
        for case_id in released_ids:
            self._active_case_ids.discard(case_id)
        host.current_rows.clear()
        host.current_index = 0
        host.current_batch_id = None
        self._transition_host(host, "disconnected")
        self._trace_event("host_disconnected", host=host.hostname)
        self._wake_waiting_hosts()

    def _host_reconnect(self, hostname: object) -> None:
        host = self._host(str(hostname))
        if host.enabled:
            return
        host.enabled = True
        host.epoch += 1
        self._transition_host(host, "idle")
        if not self._all_complete():
            self._schedule(self._now, "host_fetch", host.hostname, host.epoch)

    def _wake_waiting_hosts(self) -> None:
        for hostname in sorted(self._waiting_hosts):
            host = self._host(hostname)
            if host.enabled and host.state == "idle":
                self._long_poll_wake_count += 1
                self._schedule(self._now, "host_fetch", host.hostname, host.epoch)
        self._waiting_hosts.clear()

    def _compile_submission(self, node: _Node) -> CompileSubmission:
        return CompileSubmission(
            compile_key=node.compile_key,
            submit_id=domjudge_submit_id(node.compile_key),
            source_name=f"{node.program_key}.cpp",
            source_file=PayloadFile(
                path=Path(f"/__judgehost_sim__/{node.compile_key}.cpp"),
                size=0,
                identity=node.compile_key,
            ),
            extra_source_items=(),
            compile_files=(),
        )

    def _case_row(self, node: _Node) -> dict[str, object]:
        identity = _sha256(f"testcase:{node.node_id}")
        testcase_id = int(identity, 16) % (1 << 63)
        return {
            "task_id": node.task_id,
            "run_id": node.run_id,
            "test_name": f"{node.test_index:03}.in",
            "ordinal": node.test_index,
            "scope_sequence": node.scope_sequence,
            "testcase_id": testcase_id,
            "testcase_hash": identity,
            "testcase_input_hash": identity,
            "testcase_answer_hash": identity,
            "input_ref": f"blob://sha256/{identity}",
            "answer_ref": f"blob://sha256/{identity}",
            "status": "staged",
        }

    @staticmethod
    def _case_result(node: _Node):
        return build_case_result(
            test_name=f"{node.test_index:03}.in",
            runresult="correct",
            verdict="OK",
            runtime_sec=node.case_duration_sec,
            cpu_sec=node.case_duration_sec,
            wall_sec=node.case_duration_sec,
            memory_kb=1024,
            score_text="",
            output_run_ref="",
            output_error_ref="",
            output_system_ref="",
            output_diff_ref="",
            metadata_ref="",
            compare_metadata_ref="",
            team_message_ref="",
            feedback_text="",
            feedback_files=(),
            answer_correct=False,
        )

    def _report(self, makespan: float) -> dict[str, object]:
        unfinished = sorted(
            node.node_id for node in self._nodes.values() if node.state != "completed"
        )
        dangling_batches = []
        for batch_id in sorted(self._batch_ids):
            batch = self.scheduler.fetch_batch(batch_id)
            if batch is not None and str(batch["status"]) not in _TERMINAL_BATCH_STATUSES:
                dangling_batches.append(batch_id)
        if dangling_batches:
            self._violate(f"non-terminal Batches: {dangling_batches[:8]}")
        minimum_compile_keys = {
            (node.verification_id, node.compile_key)
            for node in self._nodes.values()
            if not node.cache_hit and node.state == "completed"
        }
        host_rows = []
        for host in self._hosts:
            busy = host.state_times["compile"] + host.state_times["execute"]
            utilization = 0.0 if makespan <= 0.0 else busy / makespan
            host_rows.append(
                {
                    "hostname": host.hostname,
                    "compile_sec": _round(host.state_times["compile"]),
                    "execute_sec": _round(host.state_times["execute"]),
                    "idle_sec": _round(host.state_times["idle"]),
                    "disconnected_sec": _round(host.state_times["disconnected"]),
                    "utilization": _round(utilization),
                    "compile_key_count": len(host.compile_keys_seen),
                    "program_switch_count": host.program_switch_count,
                }
            )
        verification_rows = []
        for verification in self._verifications.values():
            if verification.foreground:
                continue
            finished = verification.finished_sec
            completion = None if finished is None else finished - verification.arrival_sec
            first_progress = (
                None
                if verification.first_progress_sec is None
                else verification.first_progress_sec - verification.arrival_sec
            )
            slowdown = (
                None
                if completion is None
                else completion / max(verification.critical_path_sec, 1e-9)
            )
            verification_rows.append(
                {
                    "verification_id": verification.verification_id,
                    "arrival_sec": _round(verification.arrival_sec),
                    "first_progress_sec": _optional_round(first_progress),
                    "completion_sec": _optional_round(completion),
                    "critical_path_sec": _round(verification.critical_path_sec),
                    "slowdown": _optional_round(slowdown),
                }
            )
        foreground_rows = []
        for node in self._nodes.values():
            if node.timing_kind != "foreground":
                continue
            arrival = self._verifications[node.verification_id].arrival_sec
            ready_to_lease = None if node.leased_at is None else node.leased_at - arrival
            ready_to_compile = (
                None if node.compile_started_at is None else node.compile_started_at - arrival
            )
            foreground_rows.append(
                {
                    "verification_id": node.verification_id,
                    "arrival_sec": _round(arrival),
                    "ready_to_lease_sec": _optional_round(ready_to_lease),
                    "ready_to_compile_start_sec": _optional_round(ready_to_compile),
                    "completion_sec": _optional_round(node.completed_at),
                    "makespan_sec": _optional_round(
                        None if node.completed_at is None else node.completed_at - arrival
                    ),
                    "wait_for_current_lease_sec": _optional_round(ready_to_lease),
                    "hostname": node.leased_hostname,
                    "prior_background_batch_id": node.prior_background_batch_id,
                    "resumed_previous_background": node.resumed_previous_background,
                }
            )
        background_makespan = max(
            (
                verification.finished_sec or self._now
                for verification in self._verifications.values()
                if not verification.foreground
            ),
            default=0.0,
        )
        foreground_ready_to_lease = [
            float(row["ready_to_lease_sec"])
            for row in foreground_rows
            if row["ready_to_lease_sec"] is not None
        ]
        foreground_ready_to_compile = [
            float(row["ready_to_compile_start_sec"])
            for row in foreground_rows
            if row["ready_to_compile_start_sec"] is not None
        ]
        foreground_completion = [
            float(row["completion_sec"])
            for row in foreground_rows
            if row["completion_sec"] is not None
        ]
        foreground_makespan = [
            float(row["makespan_sec"])
            for row in foreground_rows
            if row["makespan_sec"] is not None
        ]
        average_utilization = statistics.fmean(row["utilization"] for row in host_rows)
        summary = {
            "makespan_sec": _round(makespan),
            "background_makespan_sec": _round(background_makespan),
            "foreground_ready_to_lease_sec": _round(max(foreground_ready_to_lease, default=0.0)),
            "foreground_ready_to_compile_start_sec": _round(
                max(foreground_ready_to_compile, default=0.0)
            ),
            "foreground_completion_sec": _round(max(foreground_completion, default=0.0)),
            "foreground_makespan_sec": _round(max(foreground_makespan, default=0.0)),
            "foreground_wait_for_current_lease_sec": _round(
                max(foreground_ready_to_lease, default=0.0)
            ),
            "foreground_background_lease_count": (
                self._background_leases_while_foreground_waited
            ),
            "foreground_resume_count": sum(
                row["resumed_previous_background"] is not None for row in foreground_rows
            ),
            "foreground_resume_same_batch_count": sum(
                row["resumed_previous_background"] is True for row in foreground_rows
            ),
            "case_count": len(self._nodes),
            "completed_case_count": len(self._nodes) - len(unfinished),
            "unfinished_case_count": len(unfinished),
            "cache_hit_count": self._cache_hit_count,
            "leased_case_count": len(self._leased_case_ids),
            "lease_attempt_count": self._lease_attempt_count,
            "duplicate_lease_count": self._lease_attempt_count - len(self._leased_case_ids),
            "duplicate_report_count": self._duplicate_report_count,
            "compile_count": self._compile_count,
            "minimum_compile_count": len(minimum_compile_keys),
            "duplicate_compile_count": max(0, self._compile_count - len(minimum_compile_keys)),
            "compile_time_sec": _round(self._compile_time_sec),
            "ready_to_lease_p50_sec": _round(_percentile(self._ready_to_lease_sec, 0.50)),
            "ready_to_lease_p95_sec": _round(_percentile(self._ready_to_lease_sec, 0.95)),
            "ready_to_lease_max_sec": _round(max(self._ready_to_lease_sec, default=0.0)),
            "average_host_utilization": _round(average_utilization),
            "program_switch_count": sum(row["program_switch_count"] for row in host_rows),
            "empty_fetch_count": self._empty_fetch_count,
            "poll_backoff_sec": _round(self._poll_backoff_sec),
            "long_poll_wake_count": self._long_poll_wake_count,
            "batch_count": len(self._batch_ids),
            "dangling_batch_count": len(dangling_batches),
            "max_batch_host_count": max(
                (len(hosts) for hosts in self._batch_hosts.values()),
                default=0,
            ),
            "invariant_violation_count": len(self._invariant_violations),
        }
        report: dict[str, object] = {
            "schema_version": 2,
            "workload": self.workload.name,
            "seed": self.workload.seed,
            "strategy": self.strategy,
            "summary": summary,
            "verifications": verification_rows,
            "foreground_tasks": foreground_rows,
            "hosts": host_rows,
            "batch_host_counts": {
                str(batch_id): len(hosts) for batch_id, hosts in sorted(self._batch_hosts.items())
            },
            "unfinished_cases": unfinished,
            "dangling_batches": dangling_batches,
            "invariant_violations": list(self._invariant_violations),
        }
        report["assertion_failures"] = evaluate_assertions(report, self.workload.assertions)
        if self._trace_enabled:
            report["trace"] = self._trace
        return report

    def _duration(self, duration_range: DurationRange) -> float:
        if duration_range.minimum_sec == duration_range.maximum_sec:
            return duration_range.minimum_sec
        return self._rng.uniform(duration_range.minimum_sec, duration_range.maximum_sec)

    def _schedule(self, at_sec: float, kind: str, *payload: object) -> None:
        self._event_sequence += 1
        heapq.heappush(self._events, (float(at_sec), self._event_sequence, kind, payload))

    def _trace_event(self, kind: str, **fields: object) -> None:
        if self._trace_enabled:
            self._trace.append({"at_sec": _round(self._now), "event": kind, **fields})

    def _now_text(self) -> str:
        return (_BASE_TIME + timedelta(seconds=self._now)).isoformat()

    def _host(self, hostname: str) -> _Host:
        for host in self._hosts:
            if host.hostname == hostname:
                return host
        raise KeyError(hostname)

    def _transition_host(self, host: _Host, state: str, *, at_sec: float | None = None) -> None:
        now = self._now if at_sec is None else float(at_sec)
        elapsed = max(0.0, now - host.state_since)
        host.state_times[host.state] += elapsed
        host.state = state
        host.state_since = now

    def _all_complete(self) -> bool:
        return self._remaining_node_count == 0

    def _foreground_waiting(self) -> bool:
        return any(
            node.timing_kind == "foreground"
            and node.state == "ready"
            and node.leased_at is None
            for node in self._nodes.values()
        )

    def _violate(self, message: str) -> None:
        if message not in self._invariant_violations:
            self._invariant_violations.append(message)


def run_simulation(
    workload: Workload,
    *,
    trace: bool = False,
    strategy: str = "production",
) -> dict[str, object]:
    return JudgehostSimulation(workload, trace=trace, strategy=strategy).run()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _round(value: float) -> float:
    return round(float(value), 6)


def _optional_round(value: float | None) -> float | None:
    return None if value is None else _round(value)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction
