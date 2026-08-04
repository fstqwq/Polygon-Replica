from __future__ import annotations

import json
import re
import uuid
from typing import cast
from pathlib import Path

from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_source_hash
from app.service.judgehost.limits import compile_output_kb, run_output_kb
from app.service.judgehost.identity import canonical_verification_id, compile_key
from app.service.judgehost.shared import _RUN_ID_RE, domjudge_lower_text, domjudge_path_name, domjudge_text
from app.service.judgehost.runtime import domjudge_bool, domjudge_parse_int
from app.service.platform.hashing import domjudge_executable_hash, sha256_hex_json
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.run.runtime import RUN_TEST_NAME_RE
from app.service.platform.testlib_source import workspace_testlib_header

from app.service.judgehost.core import JudgehostCore
from app.service.judgehost.dispatch import DispatchHandler
from app.service.judgehost.state import JudgehostState
from app.service.judgehost.toolkit import DomjudgeToolkit


class TaskEnqueue:
    STATUS_QUEUED = "queued"
    STATUS_ENQUEUING = "enqueuing"
    STATUS_FAILED = "failed"
    _TASK_KIND_COMPILE_ONLY = "compile-only"

    def __init__(
        self,
        state: JudgehostState,
        core: JudgehostCore,
        dispatch: DispatchHandler,
        toolkit: DomjudgeToolkit,
    ) -> None:
        self._s = state
        self._core = core
        self._dispatch = dispatch
        self._toolkit = toolkit

    _JAVA_CLASS_DECL_RE = re.compile(
        r"\b(?P<public>public\s+)?(?:(?:abstract|final|static|strictfp|sealed|non-sealed)\s+)*class\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\b"
    )
    _JAVA_MAIN_METHOD_RE = re.compile(
        r"\b(?:(?:public|protected|private|static|final|synchronized|strictfp|native)\s+)*void\s+main\s*\(",
        re.MULTILINE,
    )

    @staticmethod
    def _normalize_text(value: object) -> str:
        return "" if not value else str(value).strip()

    @staticmethod
    def _normalize_text_with_default(value: object, *, default: str) -> str:
        text = TaskEnqueue._normalize_text(value)
        return text if text else default

    @staticmethod
    def _normalize_status(value: object) -> str:
        return TaskEnqueue._normalize_text(value).lower()

    def _verification_artifact_ref(self, verification_id: str, test_name: str, ref_key: str) -> str:
        safe_verification_id = TaskEnqueue._normalize_text(verification_id)
        safe_test_name = TaskEnqueue._normalize_text(test_name)
        safe_ref_key = TaskEnqueue._normalize_text(ref_key)
        if (not safe_verification_id) or (not safe_test_name) or safe_ref_key not in {"input_ref", "answer_ref"}:
            return ""
        row = self._s.db.fetch_one(
            f"""
            SELECT {safe_ref_key}
            FROM verification_artifact_refs
            WHERE verification_id=? AND test_name=?
            """,
            [safe_verification_id, safe_test_name],
        )
        if row is None:
            return ""
        return TaskEnqueue._normalize_text(row[safe_ref_key])

    @staticmethod
    def _json_object(text: str) -> dict[str, object]:
        if not text:
            return {}
        try:
            return cast(dict[str, object], json.loads(text))
        except Exception:
            return {}

    @staticmethod
    def _normalize_list(
        values: list[str] | None,
        *,
        matcher: re.Pattern[str] | None = None,
    ) -> list[str]:
        if not values:
            return []
        normalized: list[str] = []
        for raw in values:
            token = TaskEnqueue._normalize_text(raw)
            if not token:
                continue
            if matcher is not None and not matcher.fullmatch(token):
                continue
            if token not in normalized:
                normalized.append(token)
        return normalized

    @staticmethod
    def _strip_java_noncode(source_text: str) -> str:
        text = str(source_text or "")
        out: list[str] = []
        index = 0
        size = len(text)
        while index < size:
            current = text[index]
            next_char = text[index + 1] if (index + 1) < size else ""
            if current == "/" and next_char == "/":
                out.append(" ")
                out.append(" ")
                index += 2
                while index < size and text[index] not in "\r\n":
                    out.append(" ")
                    index += 1
                continue
            if current == "/" and next_char == "*":
                out.append(" ")
                out.append(" ")
                index += 2
                while index < size:
                    char = text[index]
                    tail = text[index + 1] if (index + 1) < size else ""
                    if char == "*" and tail == "/":
                        out.append(" ")
                        out.append(" ")
                        index += 2
                        break
                    out.append("\n" if char == "\n" else " ")
                    index += 1
                continue
            if current in {'"', "'"}:
                quote = current
                out.append(" ")
                index += 1
                escaped = False
                while index < size:
                    char = text[index]
                    if char == "\n":
                        out.append("\n")
                        index += 1
                        break
                    out.append(" ")
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == quote:
                        index += 1
                        break
                    index += 1
                continue
            out.append(current)
            index += 1
        return "".join(out)

    @classmethod
    def _java_top_level_classes(cls, source_text: str) -> list[tuple[str, bool, str]]:
        stripped = cls._strip_java_noncode(source_text)
        values: list[tuple[str, bool, str]] = []
        index = 0
        size = len(stripped)
        brace_depth = 0
        while index < size:
            char = stripped[index]
            if char == "{":
                brace_depth += 1
                index += 1
                continue
            if char == "}":
                brace_depth = max(0, brace_depth - 1)
                index += 1
                continue
            if brace_depth != 0:
                index += 1
                continue
            match = cls._JAVA_CLASS_DECL_RE.match(stripped, index)
            if match is None:
                index += 1
                continue
            class_name = str(match.group("name") or "")
            if not class_name:
                index = match.end()
                continue
            body_open = stripped.find("{", match.end())
            if body_open < 0:
                index = match.end()
                continue
            nested_depth = 1
            body_index = body_open + 1
            while body_index < size and nested_depth > 0:
                token = stripped[body_index]
                if token == "{":
                    nested_depth += 1
                elif token == "}":
                    nested_depth -= 1
                body_index += 1
            if nested_depth != 0:
                index = body_index
                continue
            class_body = stripped[body_open + 1 : body_index - 1]
            values.append((class_name, bool(match.group("public")), class_body))
            index = body_index
        return values

    @classmethod
    def _java_class_has_main(cls, class_body: str) -> bool:
        stripped = cls._strip_java_noncode(class_body)
        index = 0
        size = len(stripped)
        brace_depth = 0
        while index < size:
            char = stripped[index]
            if char == "{":
                brace_depth += 1
                index += 1
                continue
            if char == "}":
                brace_depth = max(0, brace_depth - 1)
                index += 1
                continue
            if brace_depth != 0:
                index += 1
                continue
            match = cls._JAVA_MAIN_METHOD_RE.search(stripped, index)
            if match is None:
                return False
            method_start = match.start()
            if method_start < index:
                index += 1
                continue
            open_paren = stripped.find("(", match.end() - 1)
            if open_paren < 0:
                return False
            nested_depth = 1
            close_index = open_paren + 1
            while close_index < size and nested_depth > 0:
                token = stripped[close_index]
                if token == "(":
                    nested_depth += 1
                elif token == ")":
                    nested_depth -= 1
                close_index += 1
            if nested_depth != 0:
                return False
            params = stripped[open_paren + 1 : close_index - 1]
            modifiers = stripped[max(index, method_start - 128) : method_start]
            modifier_tokens = set(re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", modifiers))
            if "static" in modifier_tokens and "String" in params:
                return True
            index = close_index
        return False

    @classmethod
    def _detect_java_entry_point(cls, source_name: str, source_bytes: bytes) -> str:
        safe_source_name = Path(source_name).name
        source_text = source_bytes.decode("utf-8", errors="replace")
        top_level_classes = cls._java_top_level_classes(source_text)
        if not top_level_classes:
            raise RuntimeError(f"java entry point detection failed for {safe_source_name}: no top-level classes found")
        public_classes = [name for name, is_public, _body in top_level_classes if is_public]
        if len(public_classes) == 1:
            return public_classes[0]
        runnable_classes = [name for name, _is_public, body in top_level_classes if cls._java_class_has_main(body)]
        if len(runnable_classes) == 1:
            return runnable_classes[0]
        if len(runnable_classes) > 1:
            raise RuntimeError(
                f"java entry point detection failed for {safe_source_name}: multiple runnable classes found ({', '.join(runnable_classes)})"
            )
        raise RuntimeError(f"java entry point detection failed for {safe_source_name}: no runnable main class found")

    @classmethod
    def _normalize_submission_source(
        cls,
        *,
        source_name: str,
        source_bytes: bytes,
    ) -> tuple[str, str]:
        safe_source_name = domjudge_path_name(source_name, default="submission.cpp")
        if not safe_source_name.lower().endswith(".java"):
            return (safe_source_name, "")
        entry_point = cls._detect_java_entry_point(safe_source_name, source_bytes)
        return (f"{entry_point}.java", entry_point)

    @staticmethod
    def _enqueue_fingerprint(payload: dict[str, object]) -> str:
        stable_payload = dict(payload)
        stable_payload.pop("enqueued_at", None)
        # Precomputed executable fields contain bytes and are derived entirely
        # from the canonical descriptor-backed request payload.
        stable_payload.pop("domjudge_precomputed", None)
        return sha256_hex_json(stable_payload, ensure_ascii=False)

    def _verification_id(self, verification_id: str) -> str:
        token = TaskEnqueue._normalize_text(verification_id)
        if not token:
            token = self._s.verification_store.allocate_id()
        return canonical_verification_id(token)

    def _collect_verification_payload(
        self,
        *,
        problem: str,
        artifact_verification_id: str,
        workspace: Path,
        mode: str,
        selected_tests: list[str],
    ) -> dict[str, object]:
        if not self._s.include_build_payload:
            return {}
        safe_verification_id = TaskEnqueue._normalize_text(artifact_verification_id)
        if not safe_verification_id:
            return {}

        wanted_tests: list[str] = []
        if selected_tests:
            for raw in selected_tests:
                token = Path(TaskEnqueue._normalize_text(raw)).name
                if not RUN_TEST_NAME_RE.fullmatch(token):
                    continue
                if token in wanted_tests:
                    continue
                wanted_tests.append(token)
        else:
            selected_test_rows = self._s.db.fetch_all(
                """
                SELECT test_name
                FROM verification_selected_tests
                WHERE verification_id=?
                ORDER BY ordinal ASC
                """,
                [safe_verification_id],
            )
            for row in selected_test_rows:
                token = Path(TaskEnqueue._normalize_text(row["test_name"])).name
                if not RUN_TEST_NAME_RE.fullmatch(token):
                    continue
                if token in wanted_tests:
                    continue
                wanted_tests.append(token)
                if len(wanted_tests) >= self._s.max_tests_per_task:
                    break

        tests_payload: list[dict[str, object]] = []
        for test_name in wanted_tests:
            input_ref = TaskEnqueue._normalize_text(
                self._verification_artifact_ref(safe_verification_id, test_name, "input_ref")
            )
            if not input_ref:
                continue
            input_file = self._s.runtime_blob_store.descriptor(input_ref)
            if input_file is None:
                continue
            ans_name = f"{Path(test_name).stem}.ans"
            answer_ref = TaskEnqueue._normalize_text(
                self._verification_artifact_ref(safe_verification_id, test_name, "answer_ref")
            )
            answer_file = self._s.runtime_blob_store.put_bytes(b"")
            if answer_ref:
                resolved_answer = self._s.runtime_blob_store.descriptor(answer_ref)
                if resolved_answer is not None:
                    answer_file = resolved_answer
            tests_payload.append(
                {
                    "name": test_name,
                    "input_file": input_file.to_payload(),
                    "answer_name": ans_name,
                    "answer_file": answer_file.to_payload(),
                }
            )

        workspace_resolved = workspace.resolve()

        run_config_text = ""
        verification_row = self._s.db.fetch_one(
            "SELECT run_config_json FROM verifications WHERE id=?",
            [safe_verification_id],
        )
        if verification_row is not None:
            run_config_text = TaskEnqueue._normalize_text(verification_row["run_config_json"])

        def _safe_workspace_rel_file(rel_path: str) -> Path | None:
            token = TaskEnqueue._normalize_text(rel_path).replace("\\", "/")
            if not token:
                return None
            candidate = (workspace_resolved / token).resolve()
            if candidate == workspace_resolved or workspace_resolved not in candidate.parents:
                return None
            if candidate.is_symlink() or (not candidate.exists()) or (not candidate.is_file()):
                return None
            return candidate

        def _first_cpp_under(rel_dir: str) -> Path | None:
            base = (workspace_resolved / rel_dir).resolve()
            if base == workspace_resolved or workspace_resolved not in base.parents:
                return None
            if (not base.exists()) or (not base.is_dir()):
                return None
            for path in sorted(base.glob("*.cpp")):
                resolved = path.resolve()
                if resolved.is_symlink() or (not resolved.is_file()):
                    continue
                return resolved
            return None

        build_cfg_obj: dict[str, object] = {}
        build_cfg_path = _safe_workspace_rel_file("config/build.json")
        if build_cfg_path is not None:
            build_cfg_obj = self._json_object(build_cfg_path.read_text(encoding="utf-8", errors="replace"))
        problem_cfg_obj: dict[str, object] = {}
        problem_cfg_path = _safe_workspace_rel_file("config/problem.json")
        if problem_cfg_path is not None:
            problem_cfg_obj = self._json_object(problem_cfg_path.read_text(encoding="utf-8", errors="replace"))
        try:
            problem_time_limit_ms = int(problem_cfg_obj.get("time_limit_ms", 0))
        except Exception:
            problem_time_limit_ms = 0
        try:
            problem_memory_limit_mb = int(problem_cfg_obj.get("memory_limit_mb", 0))
        except Exception:
            problem_memory_limit_mb = 0
        if problem_time_limit_ms < 0:
            problem_time_limit_ms = 0
        if problem_memory_limit_mb < 0:
            problem_memory_limit_mb = 0

        interactive_mode = TaskEnqueue._normalize_status(mode) == "interactive"
        checker_source: Path | None = None
        if not interactive_mode:
            checker_source_token = cast(str | None, build_cfg_obj.get("checker_source"))
            if checker_source_token is None:
                checker_source_token = ""
            checker_source = _safe_workspace_rel_file(checker_source_token)
            if checker_source is None:
                checker_source = _safe_workspace_rel_file("checkers/checker.cpp")
            if checker_source is None:
                checker_source = _first_cpp_under("checkers")

        validator_source_token = cast(str | None, build_cfg_obj.get("validator_source"))
        if validator_source_token is None:
            validator_source_token = ""
        validator_source: Path | None = _safe_workspace_rel_file(validator_source_token)
        if validator_source is None:
            validator_source = _safe_workspace_rel_file("validators/validator.cpp")
        if validator_source is None:
            validator_source = _first_cpp_under("validators")

        interactor_source: Path | None = None
        if interactive_mode:
            interactor_source_token = cast(str | None, build_cfg_obj.get("interactor_source"))
            if interactor_source_token is None:
                interactor_source_token = ""
            interactor_source = _safe_workspace_rel_file(interactor_source_token)
            if interactor_source is None:
                interactor_source = _safe_workspace_rel_file("interactors/interactor.cpp")
            if interactor_source is None:
                interactor_source = _first_cpp_under("interactors")

        source_files: dict[str, Path] = {}
        if checker_source is not None:
            source_files["checker.cpp"] = checker_source
        if validator_source is not None:
            source_files["validator.cpp"] = validator_source
        if interactor_source is not None:
            source_files["interactor.cpp"] = interactor_source
        if source_files:
            testlib_source = workspace_testlib_header(workspace)
            if testlib_source is not None:
                source_files["testlib.h"] = testlib_source

        sources_payload: dict[str, dict[str, object]] = {}
        for name, source_path in source_files.items():
            descriptor = RuntimeBlobStore.describe_file(source_path)
            if descriptor.size > self._s.max_binary_payload_bytes:
                raise RuntimeError(f"{name} payload exceeds size limit")
            sources_payload[name] = self._s.runtime_blob_store.put_file(
                descriptor
            ).to_payload()

        return {
            "tests": tests_payload,
            "run_config_json": run_config_text,
            "problem_limits": {
                "time_limit_ms": int(problem_time_limit_ms),
                "memory_limit_mb": int(problem_memory_limit_mb),
                "pass_limit": domjudge_parse_int(problem_cfg_obj.get("pass_limit"), 1),
            },
            "source_files": sources_payload,
        }

    def _build_task_payload(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_file: PayloadFile | None,
        upload_filename: str | None,
        selected_tests: list[str],
        verification_id: str,
        verification_run_ids: list[str],
        logical_run_id: str,
        expected_behavior: str,
        verification_source: str,
        run_id: str,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
        verification_payload_override: dict[str, object] | None = None,
    ) -> dict[str, object]:
        workspace: Path | None = None
        if (upload_content is None and upload_file is None) or verification_payload_override is None:
            ctx = self._s.workspace_service.workspace_context(problem, username, include_recent=False)
            workspace = Path(ctx["workspace"]["path"])

        source_bytes: bytes
        source_name: str
        source_label: str
        source_file: PayloadFile
        if upload_file is not None:
            source_file = upload_file
            source_bytes = self._s.runtime_blob_store.read(
                source_file,
                max_bytes=self._s.max_source_bytes,
            )
            source_name = TaskEnqueue._normalize_text_with_default(upload_filename, default="submission.cpp")
            source_label = source_name
        elif upload_content is not None:
            source_bytes = upload_content
            source_file = self._s.runtime_blob_store.put_bytes(source_bytes)
            source_name = TaskEnqueue._normalize_text_with_default(upload_filename, default="submission.cpp")
            source_label = source_name
        else:
            if workspace is None:
                raise RuntimeError("workspace is required for submission source lookup")
            source_path = self._core.safe_workspace_source(workspace, TaskEnqueue._normalize_text(submission_path))
            source_bytes = self._core.safe_read_bytes(
                source_path,
                max_bytes=self._s.max_source_bytes,
                label="submission payload",
            )
            source_file = self._s.runtime_blob_store.put_bytes(source_bytes)
            source_name = source_path.name
            source_label = TaskEnqueue._normalize_text(submission_path) or source_name
        source_name, entry_point = self._normalize_submission_source(
            source_name=source_name,
            source_bytes=source_bytes,
        )

        if verification_payload_override is None:
            if workspace is None:
                raise RuntimeError("workspace is required for verification payload collection")
            verification_payload = self._collect_verification_payload(
                problem=problem,
                artifact_verification_id=artifact_verification_id,
                workspace=workspace,
                mode=mode,
                selected_tests=selected_tests,
            )
        else:
            verification_payload = dict(verification_payload_override)
        safe_task_kind = self._toolkit.task_kind(
            {
                "task_kind": task_kind,
                "verification_source": verification_source,
                "compile_only": bool(compile_only),
            }
        )
        compile_only_flag = safe_task_kind == self._TASK_KIND_COMPILE_ONLY
        return {
            "type": "verification.run",
            "run_id": run_id,
            "problem": problem,
            "username": username,
            "artifact_verification_id": artifact_verification_id,
            "mode": mode,
            "submission_path": TaskEnqueue._normalize_text(submission_path),
            "source_name": source_name,
            "source_label": source_label,
            "source_file": source_file.to_payload(),
            "entry_point": entry_point,
            "selected_tests": list(selected_tests),
            "verification_id": verification_id,
            "verification_run_ids": list(verification_run_ids),
            "logical_run_id": logical_run_id,
            "expected_behavior": expected_behavior,
            "verification_source": verification_source,
            "task_kind": safe_task_kind,
            "bypass_case_result_cache": bool(bypass_case_result_cache),
            "compile_only": bool(compile_only_flag),
            "verification_payload": verification_payload,
            "enqueued_at": now_iso(),
        }

    def prepare_execution_template(
        self,
        *,
        mode: str,
        upload_file: PayloadFile,
        upload_filename: str,
        verification_payload: dict[str, object],
        expected_behavior: str,
        verification_source: str,
        task_kind: str,
        extra_source_files: dict[str, PayloadFile] | None = None,
        manual_validate_only: bool = False,
        compile_only: bool = False,
    ) -> dict[str, object]:
        upload_content = self._s.runtime_blob_store.read(
            upload_file,
            max_bytes=self._s.max_source_bytes,
        )
        source_name, entry_point = self._normalize_submission_source(
            source_name=upload_filename,
            source_bytes=bytes(upload_content),
        )
        payload: dict[str, object] = {
            "mode": mode,
            "source_name": source_name,
            "source_file": upload_file.to_payload(),
            "entry_point": entry_point,
            "verification_payload": dict(verification_payload),
            "expected_behavior": expected_behavior,
            "verification_source": verification_source,
            "task_kind": task_kind,
            "compile_only": bool(compile_only),
        }
        if extra_source_files:
            payload["extra_source_files"] = {
                name: source.to_payload()
                for name, source in extra_source_files.items()
            }
        if manual_validate_only:
            payload["manual_validate_only"] = True
        return self._domjudge_precomputed_fields_from_payload(payload)

    def _domjudge_precomputed_fields_from_payload(self, payload: dict[str, object]) -> dict[str, object]:
        source_name = domjudge_path_name(payload.get("source_name"), default="submission.cpp")
        source_file = PayloadFile.from_payload(payload["source_file"])
        source_bytes = self._s.runtime_blob_store.read(
            source_file,
            max_bytes=self._s.max_source_bytes,
        )
        if not source_bytes:
            raise RuntimeError("submission source payload is empty")
        entry_point = domjudge_text(payload.get("entry_point"))
        extra_sources_obj = cast(dict[str, object] | None, payload.get("extra_source_files"))
        if extra_sources_obj is None:
            extra_sources_obj = {}
        extra_source_items: list[tuple[str, bytes]] = []
        for raw_name, raw_file in sorted(extra_sources_obj.items(), key=lambda item: TaskEnqueue._normalize_text(item[0])):
            safe_name = domjudge_path_name(raw_name)
            if (not safe_name) or safe_name == source_name:
                continue
            descriptor = PayloadFile.from_payload(raw_file)
            blob = self._s.runtime_blob_store.read(
                descriptor,
                max_bytes=self._s.max_source_bytes,
            )
            if not blob:
                continue
            extra_source_items.append((safe_name, blob))
        verification_payload = cast(dict[str, object] | None, payload.get("verification_payload"))
        if verification_payload is None:
            raise RuntimeError("verification payload is required for DOMjudge compatibility")
        run_cfg_obj: dict[str, object] = {}
        run_cfg_raw = domjudge_text(verification_payload.get("run_config_json"))
        if run_cfg_raw:
            run_cfg_obj = self._json_object(run_cfg_raw)
        problem_limits_obj = cast(dict[str, object] | None, verification_payload.get("problem_limits"))
        if problem_limits_obj is None:
            problem_limits_obj = {}
        checker_args_raw = cast(list[object] | None, run_cfg_obj.get("checker_args"))
        checker_args: list[str] = []
        if checker_args_raw is not None:
            for item in checker_args_raw:
                token = domjudge_text(item)
                if token:
                    checker_args.append(token)
        mode = domjudge_lower_text(payload.get("mode"), default="pass-fail")
        compile_only, generate_mode, main_correct = self._toolkit.execution_modes(payload)
        manual_validate_only = domjudge_bool(payload.get("manual_validate_only"), default=False)
        configured_pass_limit = max(
            1,
            domjudge_parse_int(
                run_cfg_obj.get("pass_limit"),
                domjudge_parse_int(problem_limits_obj.get("pass_limit"), 1),
            ),
        )
        pass_limit = configured_pass_limit
        compile_timeout = max(1, int(getattr(self._s.constants, "TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120) or 120))
        compile_mem_mb = max(64, int(getattr(self._s.constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048))
        compile_output_limit_kb = compile_output_kb(self._s.constants)
        run_output_limit_kb = run_output_kb(self._s.constants)
        run_process_limit = max(
            1,
            int(getattr(self._s.constants, "RUN_EXEC_PROCESS_LIMIT", 1024) or 1024),
        )
        default_cfg = getattr(self._s.constants, "GENERAL_CONFIG_DEFAULTS", {}) or {}
        run_tl_ms = domjudge_parse_int(
            run_cfg_obj.get("time_limit_ms"),
            domjudge_parse_int(
                problem_limits_obj.get("time_limit_ms"),
                domjudge_parse_int(default_cfg.get("time_limit_ms", 2000), 2000),
            ),
        )
        run_mem_mb = domjudge_parse_int(
            run_cfg_obj.get("memory_limit_mb"),
            domjudge_parse_int(
                problem_limits_obj.get("memory_limit_mb"),
                domjudge_parse_int(default_cfg.get("memory_limit_mb", 1024), 1024),
            ),
        )
        run_tl_ms = max(100, run_tl_ms)
        run_mem_mb = max(16, run_mem_mb)
        run_tl_sec = max(0.1, float(run_tl_ms) / 1000.0)
        run_overshoot_sec = 0.0
        run_mem_kb = max(16 * 1024, int(run_mem_mb * 1024))
        sources_files = verification_payload.get("source_files")
        sources_obj = cast(dict[str, object] | None, sources_files)
        if sources_obj is None:
            sources_obj = {}

        def _source_bytes(name: str) -> bytes:
            raw_file = sources_obj.get(name)
            if raw_file is None:
                return b""
            return self._s.runtime_blob_store.read(
                PayloadFile.from_payload(raw_file),
                max_bytes=self._s.max_binary_payload_bytes,
            )

        checker_source_bytes = _source_bytes("checker.cpp")
        validator_source_bytes = _source_bytes("validator.cpp")
        interactor_source_bytes = _source_bytes("interactor.cpp")
        testlib_header_bytes = _source_bytes("testlib.h")
        if checker_source_bytes:
            checker_source_bytes = self._toolkit.force_cpp_define(checker_source_bytes)
        if validator_source_bytes:
            validator_source_bytes = self._toolkit.force_cpp_define(validator_source_bytes)
        if interactor_source_bytes:
            interactor_source_bytes = self._toolkit.force_cpp_define(interactor_source_bytes)
        has_interactor_payload = bool(interactor_source_bytes)
        interactive = (
            (not compile_only)
            and (not generate_mode)
            and mode == "interactive"
        )
        if (not compile_only) and (not generate_mode) and mode == "interactive" and not has_interactor_payload:
            raise RuntimeError("interactive mode requires interactor payload")

        compile_files: list[tuple[str, bytes, bool]] = [
            (
                "run",
                self._toolkit.compile_script(
                    source_name,
                    manual_validate_only=manual_validate_only,
                    compile_only=compile_only,
                ),
                True,
            )
        ]
        if self._toolkit.language_extensions(source_name)[0] == "java":
            compile_files.append(
                ("DetectMain.java", self._toolkit.load_script_asset("DetectMain.java").encode("utf-8"), False)
            )
        run_files: list[tuple[str, bytes, bool]] = []
        compare_files: list[tuple[str, bytes, bool]] = []
        if interactive:
            # DOMjudge combined run/compare wraps the provided run executable
            # itself (renames run->runjury and writes run-interactive.sh).
            # Therefore we must provide jury program as "run" here.
            if interactor_source_bytes:
                run_files.append(
                    ("build", self._toolkit.cpp_executable_build_script("interactor.cpp", role="interactor"), True)
                )
                run_files.append(("interactor.cpp", interactor_source_bytes, False))
                if testlib_header_bytes:
                    run_files.append(("testlib.h", testlib_header_bytes, False))
            else:
                raise RuntimeError("interactive mode requires interactor payload")
            compare_files.append(("run", self._toolkit.compare_script(main_correct=main_correct), True))
        else:
            run_files.append(
                (
                    "run",
                    self._toolkit.run_script(
                        False,
                        main_correct=main_correct,
                        compile_only=compile_only,
                        generate_mode=generate_mode,
                        manual_validate_only=manual_validate_only,
                    ),
                    True,
                )
            )
            if compile_only:
                compare_files.append(("run", self._toolkit.compare_script(main_correct=False), True))
            elif generate_mode:
                compare_files.append(("run", self._toolkit.compare_script(generate_mode=True), True))
                if validator_source_bytes:
                    compare_files.append(("validator.cpp", validator_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))
            else:
                compare_files.append(("run", self._toolkit.compare_script(main_correct=main_correct), True))
                if checker_source_bytes:
                    compare_files.append(("checker.cpp", checker_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))

        source_hash = domjudge_source_hash(source_name, source_bytes)
        if extra_source_items:
            hash_blobs: list[bytes] = [f"{source_name}\0".encode("utf-8") + source_bytes]
            hash_blobs.extend(f"{name}\0".encode("utf-8") + blob for name, blob in extra_source_items)
            source_hash = self._toolkit.set_hash_from_blobs(hash_blobs)
        compile_hash = domjudge_executable_hash(compile_files)
        run_hash = domjudge_executable_hash(run_files)
        compare_hash = domjudge_executable_hash(compare_files)
        toolchain_cmd_digest = self._toolkit.toolchain_cmd_digest(
            source_name,
            manual_validate_only=manual_validate_only,
        )
        compare_script_timelimit = max(1, int(run_tl_sec))
        if checker_source_bytes or validator_source_bytes:
            compare_script_timelimit = max(compare_script_timelimit, min(compile_timeout, 120))
        compile_config = {
            "hash": compile_hash,
            "toolchain_cmd_digest": toolchain_cmd_digest,
            "filter_compiler_files": False,
            "language_extensions": list(self._toolkit.language_extensions(source_name)[1]),
            "script_timelimit": compile_timeout,
            "script_memory_limit": int(compile_mem_mb * 1024),
            "script_filesize_limit": int(compile_output_limit_kb),
        }
        run_config = {
            "hash": run_hash,
            "time_limit": run_tl_sec,
            "overshoot": run_overshoot_sec,
            "memory_limit": run_mem_kb,
            "output_limit": int(run_output_limit_kb),
            "process_limit": run_process_limit,
            "entry_point": entry_point or None,
            "pass_limit": pass_limit,
            "language_id": self._toolkit.language_extensions(source_name)[0],
        }
        if source_name.lower().endswith(".java") and (not entry_point):
            run_config["entry_point"] = self._detect_java_entry_point(source_name, source_bytes)
        full_compile_key = compile_key(
            source_hash=source_hash,
            compile_hash=compile_hash,
            compile_config=compile_config,
            entry_point=cast(str | None, run_config["entry_point"]),
            memory_limit=int(run_config["memory_limit"]),
        )
        compare_config = {
            "hash": compare_hash,
            "combined_run_compare": bool(interactive),
            "compare_args": " ".join(
                [*(['--validate-input'] if manual_validate_only else []), *checker_args]
            ),
            "script_timelimit": int(compare_script_timelimit),
            "script_memory_limit": max(run_mem_kb, int(compile_mem_mb * 1024)),
            "script_filesize_limit": int(compile_output_limit_kb),
        }
        return {
            "compile_key": full_compile_key,
            "source_hash": source_hash,
            "compile_hash": compile_hash,
            "run_hash": run_hash,
            "compare_hash": compare_hash,
            "toolchain_cmd_digest": toolchain_cmd_digest,
            "compile_config": compile_config,
            "run_config": run_config,
            "compare_config": compare_config,
            "compile_files": compile_files,
            "run_files": run_files,
            "compare_files": compare_files,
            "main_correct": main_correct,
        }

    def prepare_enqueue_payload(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_file: PayloadFile | None = None,
        upload_filename: str | None,
        run_id: str,
        selected_tests: list[str] | None,
        verification_id: str,
        verification_run_ids: list[str] | None,
        logical_run_id: str | None = None,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
    ) -> dict[str, object]:
        selected = self._normalize_list(selected_tests, matcher=RUN_TEST_NAME_RE)
        verification_run_id_list = self._normalize_list(verification_run_ids, matcher=_RUN_ID_RE)
        safe_run_id = self._core.normalize_run_id(run_id)
        safe_logical_run_id = self._core.normalize_run_id(logical_run_id or safe_run_id)
        payload = self._build_task_payload(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_file=upload_file,
            upload_filename=upload_filename,
            selected_tests=selected,
            verification_id=verification_id,
            verification_run_ids=verification_run_id_list,
            logical_run_id=safe_logical_run_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            run_id=safe_run_id,
            bypass_case_result_cache=bool(bypass_case_result_cache),
            compile_only=bool(compile_only),
        )
        payload["domjudge_precomputed"] = self._domjudge_precomputed_fields_from_payload(payload)
        payload["domjudge_execution_signature"] = self._toolkit.execution_signature(payload)
        return payload

    def _initial_summary(
        self,
        *,
        run_id: str,
        task_id: str,
        mode: str,
        pass_limit: int,
        source_label: str,
        selected_tests: list[str],
        verification_id: str,
        verification_run_ids: list[str],
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        compile_only: bool = False,
    ) -> dict[str, object]:
        safe_task_kind = self._toolkit.task_kind(
            {
                "task_kind": task_kind,
                "verification_source": verification_source,
                "compile_only": bool(compile_only),
            }
        )
        summary: dict[str, object] = {
            "mode": mode,
            "pass_limit": max(1, int(pass_limit)),
            "source": source_label,
            "selected_tests": list(selected_tests),
            "selected_tests_count": len(selected_tests),
            "verification_source": TaskEnqueue._normalize_text_with_default(
                verification_source, default="run.execute"
            ),
            "task_kind": safe_task_kind,
            "tests": [],
            "compile_log": "",
            "compile_diagnostics": [],
            "toolchain_digest": "judgehost",
            "limits": {},
            "usage": {},
            "judgehost": {
                "task_id": task_id,
                "status": self.STATUS_QUEUED,
            },
        }
        if safe_task_kind == self._TASK_KIND_COMPILE_ONLY:
            summary["compile_only"] = True
        return summary

    def enqueue_task(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        mode: str,
        submission_path: str | None,
        upload_content: bytes | None,
        upload_file: PayloadFile | None = None,
        upload_filename: str | None,
        run_id: str | None = None,
        selected_tests: list[str] | None,
        verification_id: str = "",
        verification_run_ids: list[str] | None = None,
        logical_run_id: str | None = None,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
        persist_verification_run: bool = False,
        prepared_payload: dict[str, object] | None = None,
        execution_template: dict[str, object] | None = None,
        service_class: str = "background",
    ) -> str:
        safe_run_id = self._core.normalize_run_id(run_id if run_id else verification_id)
        safe_logical_run_id = self._core.normalize_run_id(logical_run_id or safe_run_id)
        safe_verification_id = self._verification_id(verification_id)
        selected = self._normalize_list(selected_tests, matcher=RUN_TEST_NAME_RE)
        verification_run_id_list = self._normalize_list(verification_run_ids, matcher=_RUN_ID_RE)
        if not verification_run_id_list:
            verification_run_id_list = [safe_run_id]
        verification_payload_override = None
        if prepared_payload is not None and "verification_payload" in prepared_payload:
            verification_payload_override = dict(
                cast(dict[str, object], prepared_payload["verification_payload"])
            )
        payload = self._build_task_payload(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_file=upload_file,
            upload_filename=upload_filename,
            selected_tests=selected,
            verification_id=safe_verification_id,
            verification_run_ids=verification_run_id_list,
            logical_run_id=safe_logical_run_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            run_id=safe_run_id,
            bypass_case_result_cache=bool(bypass_case_result_cache),
            compile_only=bool(compile_only),
            verification_payload_override=verification_payload_override,
        )
        if prepared_payload is not None:
            payload.update(dict(prepared_payload))
        payload["domjudge_precomputed"] = (
            self._domjudge_precomputed_fields_from_payload(payload)
            if execution_template is None
            else dict(execution_template)
        )
        payload["domjudge_execution_signature"] = self._toolkit.execution_signature(payload)
        safe_task_kind = self._toolkit.task_kind(payload)
        payload["run_id"] = safe_run_id
        payload["problem"] = problem
        payload["username"] = username
        payload["artifact_verification_id"] = artifact_verification_id
        payload["mode"] = mode
        payload["submission_path"] = TaskEnqueue._normalize_text(submission_path)
        payload["selected_tests"] = list(selected)
        payload["verification_id"] = safe_verification_id
        payload["verification_run_ids"] = list(verification_run_id_list)
        payload["logical_run_id"] = safe_logical_run_id
        payload["expected_behavior"] = expected_behavior
        payload["verification_source"] = verification_source
        payload["task_kind"] = safe_task_kind
        payload["bypass_case_result_cache"] = bool(bypass_case_result_cache)
        payload["compile_only"] = bool(safe_task_kind == self._TASK_KIND_COMPILE_ONLY)
        safe_service_class = service_class.strip().lower()
        if safe_service_class not in {"foreground", "background"}:
            raise RuntimeError("invalid judgehost service class")
        payload["service_class"] = safe_service_class
        enqueue_fingerprint = self._enqueue_fingerprint(payload)
        task_id = ""
        summary: dict[str, object] | None = None
        while True:
            existing_task = self._s.task_registry.get_for_run(safe_run_id)
            if existing_task is not None:
                existing_fingerprint = str(existing_task.get("enqueue_fingerprint") or "")
                if existing_fingerprint != enqueue_fingerprint:
                    raise RuntimeError("judgehost run id reused with different payload")
                existing_task_id = str(existing_task["id"])
                existing_status = TaskEnqueue._normalize_status(existing_task["status"])
                if existing_status != self.STATUS_ENQUEUING:
                    return existing_task_id
                generation = self._s.task_registry.change_generation()
                self._s.task_registry.wait_for_change(generation, 0.05)
                continue
            task_id = f"jt-{uuid.uuid4().hex[:12]}"
            source_label_obj = payload.get("source_label")
            if source_label_obj is None:
                source_label_obj = payload.get("source_name")
            source_label = str(source_label_obj) if source_label_obj is not None else "upload"
            summary = self._initial_summary(
                run_id=safe_run_id,
                task_id=task_id,
                mode=mode,
                pass_limit=max(
                    1,
                    int(
                        cast(
                            dict[str, object],
                            cast(dict[str, object], payload["domjudge_precomputed"])["run_config"],
                        ).get("pass_limit", 1)
                        or 1
                    ),
                ),
                source_label=source_label,
                selected_tests=selected,
                verification_id=safe_verification_id,
                verification_run_ids=verification_run_id_list,
                expected_behavior=expected_behavior,
                verification_source=verification_source,
                task_kind=safe_task_kind,
                compile_only=bool(safe_task_kind == self._TASK_KIND_COMPILE_ONLY),
            )
            now_text = now_iso()
            task_row = {
                "id": task_id,
                "run_id": safe_run_id,
                "problem_slug": str(problem),
                "username": str(username),
                "artifact_verification_id": str(artifact_verification_id),
                "mode": str(mode),
                "verification_id": safe_verification_id,
                "status": self.STATUS_ENQUEUING,
                "payload": dict(payload),
                "result": {},
                "persist_verification_run": bool(persist_verification_run),
                "error_text": "",
                "created_at": now_text,
                "updated_at": now_text,
                "completed_at": "",
                "summary": dict(summary),
                "enqueue_fingerprint": enqueue_fingerprint,
            }
            try:
                self._s.task_registry.insert(task_row)
            except RuntimeError as exc:
                if str(exc) != "judgehost task already exists":
                    raise
                continue
            break

        if summary is None or not task_id:
            raise RuntimeError("failed to allocate judgehost task")

        batch_id = 0
        try:
            batch_id = self._dispatch.stage_task(
                {"task_id": task_id, "run_id": safe_run_id, "payload": dict(payload)}
            )
        except Exception as exc:
            finished_at = now_iso()
            self._s.task_registry.transition(
                task_id,
                expected={self.STATUS_ENQUEUING},
                status=self.STATUS_FAILED,
                updates={
                    "result": {"run_status": "failed", "error": str(exc)},
                    "error_text": str(exc),
                    "updated_at": finished_at,
                    "completed_at": finished_at,
                },
            )
            raise
        queued = self._s.task_registry.transition(
            task_id,
            expected={self.STATUS_ENQUEUING},
            status=self.STATUS_QUEUED,
            updates={"updated_at": now_iso()},
        )
        if queued is None:
            self._s.batch_scheduler.cancel_staged_task_cases(
                task_id,
                now_text=now_iso(),
            )
            self._dispatch.finalize_batch_if_ready(
                batch_id,
                error_text="judgehost task staging lost its queued transition",
            )
            raise RuntimeError("judgehost task staging lost its queued transition")
        if not self._dispatch.activate_task_cases(task_id):
            finished_at = now_iso()
            self._s.task_registry.transition(
                task_id,
                expected={self.STATUS_QUEUED},
                status=self.STATUS_FAILED,
                updates={
                    "result": {"run_status": "failed", "error": "judgehost task staged no cases"},
                    "error_text": "judgehost task staged no cases",
                    "updated_at": finished_at,
                    "completed_at": finished_at,
                },
            )
            self._dispatch.finalize_batch_if_ready(
                batch_id,
                error_text="judgehost task staged no cases",
            )
            raise RuntimeError("judgehost task staged no cases")
        return task_id

    def enqueue_compile_only_task(
        self,
        *,
        problem: str,
        username: str,
        artifact_verification_id: str,
        upload_content: bytes,
        upload_filename: str,
        run_id: str,
        verification_id: str,
        verification_run_ids: list[str] | None = None,
        expected_behavior: str = "compile",
        verification_source: str = "compile.only",
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        return self.enqueue_task(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode="pass-fail",
            submission_path=None,
            upload_content=bytes(upload_content),
            upload_filename=upload_filename or "submission.cpp",
            run_id=run_id,
            selected_tests=[],
            verification_id=TaskEnqueue._normalize_text(verification_id),
            verification_run_ids=list(verification_run_ids or [run_id]),
            expected_behavior=expected_behavior or "compile",
            verification_source=verification_source or "compile.only",
            task_kind=self._TASK_KIND_COMPILE_ONLY,
            compile_only=True,
            persist_verification_run=False,
            prepared_payload=None if prepared_payload is None else dict(prepared_payload),
            service_class="foreground",
        )
