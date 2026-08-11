"""Official-shaped Judgehost mock for the isolated Docker E2E scenario.

This process never compiles or executes untrusted input.  It exercises the wire
shapes used by the pinned DOMjudge judgedaemon plus Polygon-Replica's explicit
idempotent-ACK and late-diagnostic extensions.  It returns deterministic case
outcomes for the fixture seeded by bootstrap.py.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from domjudge_contract import (
    COMPILE_REPORT_FIELDS,
    CONFIG_REQUIRED_FIELDS,
    ENDPOINTS,
    FINAL_REPORT_FIELDS,
    MOCK_READY_FILENAME,
    MOCK_STATE_FILENAME,
    UPSTREAM_PEELED_COMMIT,
    WORK_REQUIRED_FIELDS,
    require_approval,
    state_dir,
)


HOSTNAME = "mock-domjudge-9-0-1"


@dataclass(frozen=True)
class MockOutcome:
    output: bytes = b""
    runresult: str = "correct"
    compile_success: bool = True
    active_internal_error: bool = False
    late_diagnostics: bool = False


SOURCE_OUTCOMES = {
    "gen.py": MockOutcome(output=b"7\n"),
    "main.cpp": MockOutcome(output=b"49\n"),
    "re.py": MockOutcome(
        runresult="run-error",
        late_diagnostics=True,
    ),
    "ce.cpp": MockOutcome(compile_success=False),
    "sanity_empty_output.py": MockOutcome(runresult="wrong-answer"),
    "sanity_unicode_output.py": MockOutcome(active_internal_error=True),
}


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JudgehostMock:
    def __init__(self) -> None:
        self.approval = require_approval()
        token = os.environ["POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN"]
        self.client = httpx.Client(
            base_url=os.environ["POLYGON_REPLICA_E2E_APP_URL"].rstrip("/") + "/",
            auth=("judgehost", token),
            timeout=httpx.Timeout(15.0),
        )
        self.state_path = state_dir() / MOCK_STATE_FILENAME
        self.ready_path = state_dir() / MOCK_READY_FILENAME
        self.state: dict[str, object] = {
            "domjudge_commit": self.approval["commit"],
            "source_sha256s": self.approval["source_sha256s"],
            "hostname": HOSTNAME,
            "events": [],
            "error": "",
        }
        self.waiting = False
        self._persist()

    def _persist(self) -> None:
        _atomic_json(self.state_path, self.state)

    def _record(self, event: dict[str, object]) -> None:
        events = self.state["events"]
        if not isinstance(events, list):
            raise RuntimeError("mock event state is invalid")
        events.append(event)
        self._persist()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        # Re-read the approval before every network operation.  Removing or
        # replacing it therefore closes an already-running mock as well.
        approval = require_approval()
        if approval["commit"] != UPSTREAM_PEELED_COMMIT:
            raise RuntimeError("mock request blocked by unapproved DOMjudge commit")
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    @staticmethod
    def _json(response: httpx.Response, expected: type) -> object:
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"non-JSON response from {response.request.url}") from exc
        if not isinstance(payload, expected):
            raise RuntimeError(
                f"unexpected JSON shape from {response.request.url}: "
                f"expected {expected.__name__}, got {type(payload).__name__}"
            )
        return payload

    def initialize(self) -> None:
        registered = self._json(
            self._request(
                "POST",
                ENDPOINTS["judgehosts"],
                content=f"hostname={HOSTNAME}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            list,
        )
        config = self._json(self._request("GET", ENDPOINTS["config"]), dict)
        languages = self._json(self._request("GET", ENDPOINTS["languages"]), list)
        if not config or not languages:
            raise RuntimeError("Judgehost bootstrap responses must be non-empty")
        missing_config = [field for field in CONFIG_REQUIRED_FIELDS if field not in config]
        if missing_config:
            raise RuntimeError(f"Judgehost config omitted official fields: {missing_config!r}")
        for language in languages:
            if (
                not isinstance(language, dict)
                or not isinstance(language.get("id"), str)
                or not isinstance(language.get("extensions"), list)
            ):
                raise RuntimeError("Judgehost language response has an invalid official shape")
        self._record(
            {
                "kind": "registered",
                "returned_unfinished": len(registered),
                "language_count": len(languages),
            }
        )
        self.ready_path.touch()

    @staticmethod
    def _validate_work(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise RuntimeError("fetch-work item must be an object")
        row = dict(raw)
        missing = [field for field in WORK_REQUIRED_FIELDS if field not in row]
        if missing:
            raise RuntimeError(f"fetch-work item is missing official fields: {missing!r}")
        if row["type"] != "judging_run":
            raise RuntimeError(f"unsupported work type: {row['type']!r}")
        for field in ("judgetaskid", "jobid", "submitid", "testcase_id"):
            if int(str(row[field])) <= 0:
                raise RuntimeError(f"fetch-work field {field} must be a positive integer")
        for field in ("compile_config", "run_config", "compare_config"):
            try:
                config = json.loads(str(row[field]))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"fetch-work field {field} is not JSON") from exc
            if not isinstance(config, dict):
                raise RuntimeError(f"fetch-work field {field} must be an object")
            executable_hash = config.get("hash")
            if not isinstance(executable_hash, str) or re.fullmatch(
                r"[0-9a-f]{32}", executable_hash
            ) is None:
                raise RuntimeError(f"fetch-work field {field} lacks the executable hash")
        return row

    def _download_files(
        self,
        path: str,
        *,
        executable: bool,
        expected_hash: str = "",
    ) -> dict[str, bytes]:
        payload = cast(list[object], self._json(self._request("GET", path), list))
        if not payload:
            raise RuntimeError(f"official file-array endpoint returned no files: {path}")
        decoded: dict[str, bytes] = {}
        executable_rows: list[tuple[str, bytes, bool]] = []
        for raw in payload:
            if not isinstance(raw, dict):
                raise RuntimeError(f"file-array entry is not an object: {path}")
            filename = raw.get("filename")
            content = raw.get("content")
            if not isinstance(filename, str) or not filename or Path(filename).name != filename:
                raise RuntimeError(f"unsafe or empty file-array filename: {filename!r}")
            if not isinstance(content, str):
                raise RuntimeError(f"file-array content is not base64 text: {filename}")
            is_executable = raw.get("is_executable")
            if executable and not isinstance(is_executable, bool):
                raise RuntimeError(f"executable file-array entry lacks is_executable: {filename}")
            try:
                file_content = base64.b64decode(content.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
                raise RuntimeError(f"invalid base64 file-array content: {filename}") from exc
            if filename in decoded:
                raise RuntimeError(
                    f"file-array endpoint returned duplicate filename: {filename}"
                )
            decoded[filename] = file_content
            if executable:
                executable_rows.append((filename, file_content, bool(is_executable)))
        if executable:
            parts = [
                hashlib.md5(content).hexdigest()
                + filename
                + ("1" if is_executable else "")
                for filename, content, is_executable in sorted(executable_rows)
            ]
            computed_hash = hashlib.md5("".join(parts).encode("utf-8")).hexdigest()
            if computed_hash != expected_hash:
                raise RuntimeError(
                    f"DOMjudge executable hash mismatch for {path}: "
                    f"expected {expected_hash}, got {computed_hash}"
                )
        return decoded

    def _report_versions(self, judgetaskid: int) -> None:
        path = ENDPOINTS["version_commands"].format(judgetaskid=judgetaskid)
        commands = cast(
            dict[str, object],
            self._json(self._request("GET", path), dict),
        )
        # The real daemon executes these scripts.  A mock must not execute
        # server-supplied commands; it preserves the official conditional
        # base64 fields and PUT sequence with deterministic telemetry instead.
        report: dict[str, str] = {"hostname": HOSTNAME}
        if "compiler_version_command" in commands:
            report["compiler"] = _b64(b"mock compiler for DOMjudge 9.0.1 wire E2E\n")
        if "runner_version_command" in commands:
            report["runner"] = _b64(b"mock runner for DOMjudge 9.0.1 wire E2E\n")
        if len(report) > 1:
            check_path = ENDPOINTS["check_versions"].format(judgetaskid=judgetaskid)
            self._json(self._request("PUT", check_path, data=report), dict)

    def _compile_report(self, judgetaskid: int, *, success: bool) -> None:
        report = {
            "compile_success": "1" if success else "0",
            "output_compile": _b64(
                b"mock compile accepted\n"
                if success
                else b"mock compiler error\n"
            ),
            "compile_metadata": _b64(
                b"exitcode: 0\n" if success else b"exitcode: 1\n"
            ),
        }
        if tuple(report) != COMPILE_REPORT_FIELDS:
            raise RuntimeError("mock compile report drifted from the declared official shape")
        path = ENDPOINTS["update_judging"].format(
            hostname=HOSTNAME,
            judgetaskid=judgetaskid,
        )
        self._json(self._request("PUT", path, data=report), dict)

    def _add_debug_info(self, judgetaskid: int) -> None:
        path = ENDPOINTS["add_debug_info"].format(
            hostname=HOSTNAME,
            judgetaskid=judgetaskid,
        )
        self._json(
            self._request(
                "POST",
                path,
                files={
                    "full_debug": (
                        None,
                        _b64(b"late debug-info from mock: error evidence\n"),
                    ),
                },
            ),
            dict,
        )

    def _internal_error(self, judgetaskid: int, *, late: bool) -> int:
        description = (
            "late internal-error from mock"
            if late
            else "active internal-error from mock"
        )
        response = self._request(
            "POST",
            ENDPOINTS["internal_error"],
            data={
                "description": description,
                "judgehostlog": _b64(f"{description}\n".encode("utf-8")),
                "disabled": json.dumps(
                    {"kind": "judgehost", "hostname": HOSTNAME},
                    separators=(",", ":"),
                ),
                "hostname": HOSTNAME,
                "judgetaskid": str(judgetaskid),
            },
        )
        result = self._json(response, int)
        if type(result) is not int:
            raise RuntimeError("internal-error acknowledgement must be a JSON integer")
        return result

    def _final_report(
        self,
        row: dict[str, object],
        output: bytes,
        runresult: str,
    ) -> int:
        judgetaskid = int(str(row["judgetaskid"]))
        now = time.time()
        report = {
            "runresult": runresult,
            "start_time": f"{now - 0.002:.6f}",
            "end_time": f"{now:.6f}",
            "runtime": "0.001",
            "output_run": _b64(output),
            "output_error": _b64(
                b"mock runtime error\n" if runresult == "run-error" else b""
            ),
            "output_system": _b64(b"mock system output\n"),
            "metadata": _b64(
                b"time-used: cpu-time\n"
                b"cpu-time: 0.001\n"
                b"wall-time: 0.001\n"
                b"memory-bytes: 4096\n"
            ),
            "output_diff": _b64(
                b"accepted by mock comparator\n"
                if runresult == "correct"
                else b"wrong answer from stability probe\n"
            ),
            "hostname": HOSTNAME,
            "testcasedir": f"/mock/testcase{int(str(row['testcase_id'])):05d}",
            "compare_metadata": _b64(
                b"exitcode: 42\n" if runresult == "correct" else b"exitcode: 43\n"
            ),
        }
        if tuple(report) != FINAL_REPORT_FIELDS:
            raise RuntimeError("mock final report drifted from the declared official shape")
        multipart = {name: (None, value) for name, value in report.items()}
        path = ENDPOINTS["add_judging_run"].format(
            hostname=HOSTNAME,
            judgetaskid=judgetaskid,
        )
        response = self._request("POST", path, files=multipart)
        ack = self._json(response, int)
        if type(ack) is not int or ack != 1:
            raise RuntimeError(f"final callback acknowledgement must be JSON integer 1, got {ack!r}")
        return ack

    def process(self, row: dict[str, object]) -> None:
        judgetaskid = int(str(row["judgetaskid"]))
        self._report_versions(judgetaskid)
        executable_hashes = {
            file_type: str(json.loads(str(row[f"{file_type}_config"]))["hash"])
            for file_type in ("compile", "run", "compare")
        }

        source_path = ENDPOINTS["source"].format(submitid=row["submitid"])
        sources = self._download_files(source_path, executable=False)
        output_candidates = {
            filename: SOURCE_OUTCOMES[filename]
            for filename in sources
            if filename in SOURCE_OUTCOMES
        }
        if len(output_candidates) != 1:
            raise RuntimeError(
                "fixture source cannot be mapped to one deterministic mock output: "
                f"{sorted(sources)!r}"
            )
        source_name, outcome = next(iter(output_candidates.items()))
        source_sha256s = {
            filename: hashlib.sha256(content).hexdigest()
            for filename, content in sorted(sources.items())
        }

        executable_names: dict[str, list[str]] = {}
        compile_path = ENDPOINTS["executable"].format(
            file_type="compile",
            script_id=row["compile_script_id"],
        )
        executable_names["compile"] = sorted(
            self._download_files(
                compile_path,
                executable=True,
                expected_hash=executable_hashes["compile"],
            )
        )
        self._compile_report(judgetaskid, success=outcome.compile_success)
        if not outcome.compile_success:
            self._record(
                {
                    "kind": "compile-error",
                    "judgetaskid": judgetaskid,
                    "source": source_name,
                    "source_files": sorted(sources),
                    "source_sha256s": source_sha256s,
                    "executable_files": executable_names,
                }
            )
            return

        if outcome.active_internal_error:
            internal_error_ack = self._internal_error(judgetaskid, late=False)
            self._record(
                {
                    "kind": "internal-error",
                    "judgetaskid": judgetaskid,
                    "source": source_name,
                    "source_files": sorted(sources),
                    "source_sha256s": source_sha256s,
                    "executable_files": executable_names,
                    "internal_error_ack": internal_error_ack,
                }
            )
            return

        testcase_path = ENDPOINTS["testcase"].format(testcase_id=row["testcase_id"])
        testcase_files = self._download_files(testcase_path, executable=False)
        if not {"input", "output"}.issubset(testcase_files):
            raise RuntimeError("testcase download must contain input and output")
        testcase_sha256s = {
            filename: hashlib.sha256(content).hexdigest()
            for filename, content in sorted(testcase_files.items())
        }

        for file_type, id_field in (
            ("run", "run_script_id"),
            ("compare", "compare_script_id"),
        ):
            path = ENDPOINTS["executable"].format(
                file_type=file_type,
                script_id=row[id_field],
            )
            executable_names[file_type] = sorted(
                self._download_files(
                    path,
                    executable=True,
                    expected_hash=executable_hashes[file_type],
                )
            )

        ack = self._final_report(row, outcome.output, outcome.runresult)
        duplicate_ack = self._final_report(
            row,
            outcome.output,
            outcome.runresult,
        )
        late_debug = False
        late_internal_error_ack: int | None = None
        duplicate_late_internal_error_ack: int | None = None
        if outcome.late_diagnostics:
            self._add_debug_info(judgetaskid)
            self._add_debug_info(judgetaskid)
            late_debug = True
            late_internal_error_ack = self._internal_error(judgetaskid, late=True)
            duplicate_late_internal_error_ack = self._internal_error(
                judgetaskid,
                late=True,
            )
        self._record(
            {
                "kind": "completed",
                "judgetaskid": judgetaskid,
                "source": source_name,
                "source_files": sorted(sources),
                "source_sha256s": source_sha256s,
                "runresult": outcome.runresult,
                "executable_files": executable_names,
                "testcase_files": sorted(testcase_files),
                "testcase_sha256s": testcase_sha256s,
                "output_sha256": hashlib.sha256(outcome.output).hexdigest(),
                "ack": ack,
                "duplicate_ack": duplicate_ack,
                "late_debug": late_debug,
                "late_internal_error_ack": late_internal_error_ack,
                "duplicate_late_internal_error_ack": (
                    duplicate_late_internal_error_ack
                ),
            }
        )

    def run(self) -> None:
        self.initialize()
        while True:
            response = self._request(
                "POST",
                ENDPOINTS["fetch_work"],
                files={"hostname": (None, HOSTNAME)},
            )
            work = cast(list[object], self._json(response, list))
            if not work:
                if not self.waiting:
                    hosts = cast(
                        list[object],
                        self._json(
                            self._request("GET", ENDPOINTS["judgehosts"]),
                            list,
                        ),
                    )
                    registered = next(
                        (
                            host
                            for host in hosts
                            if isinstance(host, dict)
                            and host.get("hostname") == HOSTNAME
                        ),
                        None,
                    )
                    if registered is None or registered.get("enabled") is not True:
                        raise RuntimeError("registered mock Judgehost is not enabled")
                    self.waiting = True
                time.sleep(0.05)
                continue
            self.waiting = False
            for raw in work:
                self.process(self._validate_work(raw))


def main() -> None:
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    ready = state / MOCK_READY_FILENAME
    ready.unlink(missing_ok=True)
    mock: JudgehostMock | None = None
    try:
        mock = JudgehostMock()
        mock.run()
    except BaseException as exc:
        ready.unlink(missing_ok=True)
        if mock is not None:
            mock.state["error"] = f"{type(exc).__name__}: {exc}"
            mock._persist()
        raise
    finally:
        if mock is not None:
            mock.client.close()


if __name__ == "__main__":
    main()
