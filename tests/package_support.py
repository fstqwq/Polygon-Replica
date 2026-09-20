"""Real published-source and durable verification fixtures for package consumers."""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.main import runtime
from app.service.execution.model import (
    CAPTURE_COMPLETE,
    ExecutionPassResult,
    ExecutionUsage,
    PassArtifacts,
)
from app.service.execution.policy import normalize_execution_result
from app.service.problem.build_config import dumps_build_config, load_build_config
from app.service.problem_package.service import VerificationBuilder
from app.service.platform.worker_queue import WorkerQueueService
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus
from tests.common import configure_interactive_workspace
from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_fetch_one,
    verification_programs_for_tasks,
)
from tests.execution_result_helpers import execution_result


def publish_problem(
    workspace: Path,
    problem_slug: str,
    actor_username: str,
    *,
    test_id: str = "001",
    test_ids: tuple[str, ...] | None = None,
    extra_solutions: dict[str, str] | None = None,
    mode: str = "pass-fail",
) -> tuple[Path, int, str]:
    selected_test_ids = (test_id,) if test_ids is None else test_ids
    if mode == "interactive":
        configure_interactive_workspace(
            workspace,
            time_limit_ms=2000,
            memory_limit_mb=1024,
            pass_limit=1,
        )
    elif mode != "pass-fail":
        raise AssertionError(f"unsupported fixture mode: {mode}")
    (workspace / "tests" / "manual").mkdir(parents=True, exist_ok=True)
    for selected_test_id in selected_test_ids:
        (workspace / "tests" / "manual" / f"{selected_test_id}.in").write_text(
            "1\n",
            encoding="utf-8",
        )
    (workspace / "tests" / "spec.json").write_text(
        json.dumps(
            {
                "tests": [
                    {
                        "id": selected_test_id,
                        "kind": "manual",
                        "sample": True,
                        "sample_input": "display input\n",
                        "sample_output": "display output\n",
                    }
                    for selected_test_id in selected_test_ids
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    accepted = workspace / "solutions" / "accepted.cpp"
    accepted.write_text("int main() { return 0; }\n", encoding="utf-8")
    for filename, expected_behavior in (extra_solutions or {}).items():
        source = workspace / "solutions" / filename
        source.write_text("int main() { return 0; }\n", encoding="utf-8")
        source.with_name(f"{source.name}.desc").write_text(
            f"expected: {expected_behavior}\n",
            encoding="utf-8",
        )
    build = load_build_config(workspace)
    build["accepted_solution_source"] = "solutions/accepted.cpp"
    (workspace / "config" / "build.json").write_text(
        dumps_build_config(build),
        encoding="utf-8",
    )
    commit = runtime.git_service.commit(
        workspace,
        "publish Native Package fixture",
        actor_username,
        f"{actor_username}@polygonlike.local",
    )
    runtime.git_service.push(workspace, "main")
    context = runtime.workspace_service.workspace_context(
        problem_slug,
        actor_username,
    )
    return workspace, int(context["problem"]["id"]), commit


@contextmanager
def blocked_export_queue() -> Iterator[threading.Event]:
    """Hold one real worker so queued exports can be observed before execution."""
    queue = WorkerQueueService(worker_count=1)
    previous_queue = runtime.worker_queue_service
    entered = threading.Event()
    release = threading.Event()

    def occupy_worker() -> None:
        entered.set()
        if not release.wait(timeout=30):
            raise AssertionError("export worker was not released")

    runtime.worker_queue_service = queue
    blocker, accepted, _reason = queue.submit(name="hold-export-worker", fn=occupy_worker)
    try:
        if not accepted or not entered.wait(timeout=5):
            raise AssertionError("export worker did not start")
        yield release
    finally:
        release.set()
        try:
            blocker.join(timeout=5)
            with runtime.export_lock:
                futures = tuple(runtime.export_workers)
            for future in futures:
                future.join(timeout=20)
                if future.is_alive():
                    raise AssertionError("export worker did not finish")
        finally:
            queue.stop()
            runtime.worker_queue_service = previous_queue


def verification_builder(
    problem_id: int,
    *,
    input_bytes: bytes = b"1\n",
    answer_bytes: bytes | None = b"2\n",
    test_ids: tuple[str, ...] = ("001",),
    solution_verdicts: dict[str, tuple[str, str]] | None = None,
    mode: str = "pass-fail",
    pre_skipped_ordinals: frozenset[int] = frozenset(),
    verification_kind: str = "all",
) -> VerificationBuilder:
    def build(
        _snapshot: Path,
        commit: str,
        _revision_number: int,
        verification_id: str,
    ) -> str:
        build_row = db_fetch_one(
            """SELECT status,phase FROM problem_package_builds
               WHERE verification_id=?""",
            [verification_id],
        )
        if build_row is not None and (
            str(build_row["status"]),
            str(build_row["phase"]),
        ) != ("running", "verification"):
            raise AssertionError("Native Package build phase is not verification")
        admission = admit_test_verification(
            verification_id=verification_id,
            problem_id=problem_id,
            workspace_id=None,
            signature="native-package-test",
            source_commit=commit,
            kind=verification_kind,
        )
        if admission.outcome != "admitted":
            raise AssertionError(f"unexpected admission outcome: {admission.outcome}")
        tasks: list[PlannedTask] = []
        generator_completions: list[TaskCompletion] = []
        run_completions: list[TaskCompletion] = []
        for ordinal, test_id in enumerate(test_ids, start=1):
            test_name = f"{ordinal:03d}.in"
            input_ref = (runtime.runtime_blob_store.put_bytes(input_bytes).blob_ref or "")
            answer_ref = ""
            if answer_bytes is not None:
                answer_ref = (runtime.runtime_blob_store.put_bytes(answer_bytes).blob_ref or "")
            generator_id = verification_task_id(
                verification_id,
                f"generator-{ordinal}",
                test_name,
            )
            tasks.append(
                PlannedTask(
                    task_id=generator_id,
                    predecessor_task_id=None,
                    task_kind="generate-input",
                    source_path=f"tests/manual/{test_id}.in",
                    program_id=f"generator-{ordinal}",
                    test_name=test_name,
                    expected_behavior="accepted",
                )
            )
            generator_completions.append(
                TaskCompletion(
                    task_id=generator_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result(
                        "SK" if ordinal in pre_skipped_ordinals else "OK",
                        output_ref=input_ref,
                    ),
                    input_ref=input_ref,
                )
            )

            accepted_id = verification_task_id(
                verification_id,
                "accepted",
                test_name,
            )
            captured_ref = answer_ref or input_ref
            tasks.append(
                PlannedTask(
                    task_id=accepted_id,
                    predecessor_task_id=generator_id,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name=test_name,
                    expected_behavior="accepted",
                )
            )
            run_completions.append(
                TaskCompletion(
                    task_id=accepted_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=normalize_execution_result(
                        passes=(
                            ExecutionPassResult(
                                number=1,
                                capture_status=CAPTURE_COMPLETE,
                                runresult="correct",
                                verdict="OK",
                                score_text="",
                                answer_correct=True,
                                usage=ExecutionUsage(),
                                feedback="",
                                artifacts=PassArtifacts(
                                    input_ref=input_ref,
                                    output_ref=captured_ref,
                                    stderr_ref=captured_ref,
                                    system_ref=captured_ref,
                                    judge_message_ref=captured_ref,
                                    team_message_ref=captured_ref,
                                    metadata_ref=captured_ref,
                                    compare_metadata_ref=captured_ref,
                                ),
                            ),
                        ),
                        verdict="OK",
                        answer_correct=True,
                    ),
                    answer_ref=answer_ref,
                )
            )
            for index, (
                source_path,
                (expected_behavior, verdict),
            ) in enumerate((solution_verdicts or {}).items(), start=1):
                program_id = f"solution-{index}"
                solution_task_id = verification_task_id(
                    verification_id,
                    program_id,
                    test_name,
                )
                tasks.append(
                    PlannedTask(
                        task_id=solution_task_id,
                        predecessor_task_id=accepted_id,
                        task_kind="solution-run",
                        source_path=source_path,
                        program_id=program_id,
                        test_name=test_name,
                        expected_behavior=expected_behavior,
                    )
                )
                run_completions.append(
                    TaskCompletion(
                        task_id=solution_task_id,
                        status=VerificationTaskStatus.DONE,
                        run_id="",
                        judgehost_task_id="",
                        result=execution_result(verdict),
                    )
                )
        activation = activate_test_verification(
            verification_id,
            programs=verification_programs_for_tasks(tasks),
            tasks=tasks,
            detail={
                "verification_id": verification_id,
                "task_graph": True,
                "mode": mode,
                "pass_limit": 1,
                "tests_meta_rows": [
                    {
                        "index": ordinal,
                        "test_name": f"{ordinal:03d}.in",
                        "kind": "manual",
                        "id": test_id,
                        "sample": True,
                    }
                    for ordinal, test_id in enumerate(test_ids, start=1)
                ],
            },
        )
        if activation.outcome != "activated":
            raise AssertionError(f"unexpected activation outcome: {activation.outcome}")
        runtime.verification_task_store.commit_task_completions(
            generator_completions
        )
        remaining: list[TaskCompletion] = []
        for task_completion in run_completions:
            task_row = db_fetch_one(
                "SELECT final_status FROM verification_tasks WHERE id=?",
                [task_completion.task_id],
            )
            if task_row is None:
                raise AssertionError(
                    f"verification task disappeared: {task_completion.task_id}"
                )
            if not str(task_row["final_status"]):
                remaining.append(task_completion)
        completion = runtime.verification_task_store.commit_task_completions(remaining)
        if completion.parent_transition != "ok":
            raise AssertionError(
                "unexpected verification transition: "
                f"{completion.parent_transition}"
            )
        return verification_id

    return build
