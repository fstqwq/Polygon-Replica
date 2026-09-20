"""A deterministic external judgehost peer for real workflow/service tests."""

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from app.service.judgehost.api import Judgehost
from app.service.judgehost.domjudge.wire_model import DomjudgeWork


@dataclass(frozen=True)
class JudgehostReply:
    output: bytes = b""
    runresult: str = "correct"
    compile_success: bool = True


@contextmanager
def reporting_judgehost(
    service: Judgehost,
    reply: Callable[[DomjudgeWork], JudgehostReply],
) -> Iterator[None]:
    """Lease real work and report ordinary one-pass results at the peer boundary."""
    hostname = f"fixture-{uuid.uuid4().hex[:12]}"
    stopped = threading.Event()
    failures: list[BaseException] = []
    service.domjudge_register_host(hostname)

    def consume() -> None:
        try:
            while not stopped.is_set():
                work_rows = service.domjudge_fetch_work(hostname)
                if not work_rows:
                    stopped.wait(0.01)
                for work in work_rows:
                    response = reply(work)
                    case_id = work["judgetaskid"]
                    service.domjudge_update_judging(
                        hostname,
                        case_id,
                        {"compile_success": "1" if response.compile_success else "0"},
                    )
                    if response.compile_success:
                        service.domjudge_add_judging_run(
                            hostname,
                            case_id,
                            {
                                "runresult": response.runresult,
                                "runtime": "0.001",
                                "output_run": response.output,
                                "metadata": (
                                    b"time-used:cpu-time\ncpu-time:0.001\n"
                                    b"wall-time:0.001\nmemory-bytes:1024\n"
                                ),
                                "compare_metadata": (
                                    b"exitcode:42\n" if response.runresult == "correct" else b"exitcode:43\n"
                                ),
                            },
                        )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("fixture judgehost did not stop")
        if failures:
            raise AssertionError("fixture judgehost failed") from failures[0]
