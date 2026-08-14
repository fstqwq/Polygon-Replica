from app.main_constant import GENERAL_CONFIG_DEFAULTS, RUN_TEST_NAME_RE

import json
import re
from typing import cast
from pathlib import Path

from app.db import now_iso
from app.service.judgehost.domjudge.cache import executable_hash, submission_source_hash
from app.service.judgehost.domjudge.limits import (
    config_int,
    compile_output_kb,
    run_memory_limit_kb,
    run_output_kb,
)
from app.service.judgehost.domjudge.identity import compile_key
from app.service.judgehost.domjudge.codec import decode_basename, decode_text
from app.service.judgehost.domjudge.result import parse_bool, parse_int
from app.service.platform.hashing import sha256_hex_json
from app.service.platform.runtime_blob_store import PayloadFile, RuntimeBlobStore
from app.service.problem.build_config import load_build_config
from app.service.problem.runtime_config import (
    load_problem_config,
)
from app.service.problem.source_file import resolve_source
from app.service.platform.testlib_source import workspace_testlib_header

from app.service.judgehost.configuration import (
    JudgehostConfiguration,
    JudgehostSettings,
)
from app.service.judgehost.validation import (
    normalize_run_id,
    normalize_verification_program_id,
    read_bounded_file,
    safe_workspace_source,
)
from app.service.judgehost.ports.case_binding import CaseBindingPort
from app.service.judgehost.domjudge.cache import blob_set_hash
from app.service.judgehost.domjudge.scripts import (
    DomjudgeScriptCatalog,
    language_extensions,
)
from app.service.judgehost.domjudge import task_plan
from app.service.repository.workspace import WorkspaceService


class JudgehostPayloadPreparation:
    _TASK_KIND_COMPILE_ONLY = "compile-only"

    def __init__(
        self,
        workspace_service: WorkspaceService,
        runtime_blob_store: RuntimeBlobStore,
        execution_port: CaseBindingPort,
        scripts: DomjudgeScriptCatalog,
        configuration: JudgehostConfiguration,
    ) -> None:
        self._workspace_service = workspace_service
        self._runtime_blob_store = runtime_blob_store
        self._execution_port = execution_port
        self._scripts = scripts
        self._configuration = configuration

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
        text = JudgehostPayloadPreparation._normalize_text(value)
        return text if text else default

    @staticmethod
    def _normalize_status(value: object) -> str:
        return JudgehostPayloadPreparation._normalize_text(value).lower()

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
            token = JudgehostPayloadPreparation._normalize_text(raw)
            if not token:
                continue
            if matcher is not None and not matcher.fullmatch(token):
                continue
            if token not in normalized:
                normalized.append(token)
        return normalized

    def normalize_tests(self, values: list[str] | None) -> list[str]:
        return self._normalize_list(values, matcher=RUN_TEST_NAME_RE)

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
            raise RuntimeError(
                f"java entry point detection failed for {safe_source_name}: no top-level classes found"
            )
        public_classes = [name for name, is_public, _body in top_level_classes if is_public]
        if len(public_classes) == 1:
            return public_classes[0]
        runnable_classes = [
            name for name, _is_public, body in top_level_classes if cls._java_class_has_main(body)
        ]
        if len(runnable_classes) == 1:
            return runnable_classes[0]
        if len(runnable_classes) > 1:
            raise RuntimeError(
                f"java entry point detection failed for {safe_source_name}: multiple runnable classes found ({', '.join(runnable_classes)})"
            )
        raise RuntimeError(
            f"java entry point detection failed for {safe_source_name}: no runnable main class found"
        )

    @classmethod
    def _normalize_submission_source(
        cls,
        *,
        source_name: str,
        source_bytes: bytes,
    ) -> tuple[str, str]:
        safe_source_name = decode_basename(raw=source_name, default="submission.cpp")
        if not safe_source_name.lower().endswith(".java"):
            return (safe_source_name, "")
        entry_point = cls._detect_java_entry_point(safe_source_name, source_bytes)
        return (f"{entry_point}.java", entry_point)

    @staticmethod
    def enqueue_fingerprint(payload: dict[str, object]) -> str:
        stable_payload = dict(payload)
        stable_payload.pop("enqueued_at", None)
        # Precomputed executable fields contain bytes and are derived entirely
        # from the canonical descriptor-backed request payload.
        stable_payload.pop("precomputed", None)
        return sha256_hex_json(stable_payload, ensure_ascii=False)

    @staticmethod
    def precomputed_pass_limit(payload: dict[str, object]) -> int:
        precomputed = payload.get("precomputed")
        if not isinstance(precomputed, dict):
            raise RuntimeError("precomputed execution payload is required")
        run_config = precomputed.get("run_config")
        if not isinstance(run_config, dict):
            raise RuntimeError("precomputed run configuration is required")
        pass_limit = run_config.get("pass_limit")
        if isinstance(pass_limit, bool) or not isinstance(pass_limit, int) or pass_limit < 1:
            raise RuntimeError("precomputed pass limit must be a positive integer")
        return pass_limit

    def verification_id(self, verification_id: str) -> str:
        token = JudgehostPayloadPreparation._normalize_text(verification_id)
        if not token:
            raise RuntimeError("execution scope id is required")
        return token

    def _collect_verification_payload(
        self,
        *,
        artifact_verification_id: str,
        workspace: Path,
        mode: str,
        selected_tests: list[str],
        settings: JudgehostSettings,
    ) -> dict[str, object]:
        safe_verification_id = JudgehostPayloadPreparation._normalize_text(artifact_verification_id)
        if not safe_verification_id:
            return {}

        wanted_tests: list[str] = []
        if selected_tests:
            for raw in selected_tests:
                token = Path(JudgehostPayloadPreparation._normalize_text(raw)).name
                if not RUN_TEST_NAME_RE.fullmatch(token):
                    continue
                if token in wanted_tests:
                    continue
                wanted_tests.append(token)
        artifact_set = self._execution_port.load_artifacts(
            safe_verification_id,
            tuple(wanted_tests),
            limit=settings.max_tests_per_task,
        )

        tests_payload: list[dict[str, object]] = []
        for artifact in artifact_set.cases:
            test_name = artifact.test_name
            input_ref = artifact.input_ref
            if not input_ref:
                continue
            input_file = self._runtime_blob_store.descriptor(input_ref)
            if input_file is None:
                continue
            ans_name = f"{Path(test_name).stem}.ans"
            answer_ref = artifact.answer_ref
            answer_file = self._runtime_blob_store.put_bytes(b"")
            if answer_ref:
                resolved_answer = self._runtime_blob_store.descriptor(answer_ref)
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

        run_config_text = artifact_set.run_config_json

        build_cfg = load_build_config(workspace)
        problem_cfg = load_problem_config(
            workspace,
            limits=settings.problem_config_limits,
        )
        problem_time_limit_ms = problem_cfg["time_limit_ms"]
        problem_memory_limit_mb = problem_cfg["memory_limit_mb"]

        requested_mode = JudgehostPayloadPreparation._normalize_status(mode)
        if requested_mode != problem_cfg["mode"]:
            raise RuntimeError("execution mode does not match config/problem.json")
        interactive_mode = requested_mode == "interactive"
        checker_source: Path | None = None
        if not interactive_mode:
            checker_source_token = build_cfg.get("checker_source")
            if checker_source_token is not None:
                checker_source = resolve_source(workspace, checker_source_token)

        validator_source_token = build_cfg.get("validator_source")
        validator_source = (
            None
            if validator_source_token is None
            else resolve_source(workspace, validator_source_token)
        )

        interactor_source: Path | None = None
        if interactive_mode:
            interactor_source_token = build_cfg.get("interactor_source")
            if interactor_source_token is None:
                raise RuntimeError("interactor source is required for interactive mode")
            interactor_source = resolve_source(workspace, interactor_source_token)

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
            if descriptor.size > settings.max_component_source_bytes:
                raise RuntimeError(f"{name} payload exceeds size limit")
            sources_payload[name] = self._runtime_blob_store.put_file(descriptor).to_payload()

        return {
            "tests": tests_payload,
            "run_config_json": run_config_text,
            "problem_limits": {
                "time_limit_ms": int(problem_time_limit_ms),
                "memory_limit_mb": int(problem_memory_limit_mb),
                "pass_limit": problem_cfg["pass_limit"],
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
        verification_task_id: str,
        verification_program_id: str,
        expected_behavior: str,
        verification_source: str,
        run_id: str,
        settings: JudgehostSettings,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
        verification_payload_override: dict[str, object] | None = None,
    ) -> dict[str, object]:
        workspace: Path | None = None
        if (
            upload_content is None and upload_file is None
        ) or verification_payload_override is None:
            ctx = self._workspace_service.workspace_context(
                problem, username, include_recent=False
            )
            workspace = Path(ctx["workspace"]["path"])

        source_bytes: bytes
        source_name: str
        source_label: str
        source_file: PayloadFile
        if upload_file is not None:
            source_file = upload_file
            source_bytes = self._runtime_blob_store.read(
                source_file,
                max_bytes=settings.max_submission_source_bytes,
            )
            source_name = JudgehostPayloadPreparation._normalize_text_with_default(
                upload_filename, default="submission.cpp"
            )
            source_label = source_name
        elif upload_content is not None:
            source_bytes = upload_content
            source_file = self._runtime_blob_store.put_bytes(source_bytes)
            source_name = JudgehostPayloadPreparation._normalize_text_with_default(
                upload_filename, default="submission.cpp"
            )
            source_label = source_name
        else:
            if workspace is None:
                raise RuntimeError("workspace is required for submission source lookup")
            source_path = safe_workspace_source(
                workspace, JudgehostPayloadPreparation._normalize_text(submission_path)
            )
            source_bytes = read_bounded_file(
                source_path,
                max_bytes=settings.max_submission_source_bytes,
                label="submission payload",
            )
            source_file = self._runtime_blob_store.put_bytes(source_bytes)
            source_name = source_path.name
            source_label = JudgehostPayloadPreparation._normalize_text(submission_path) or source_name
        source_name, entry_point = self._normalize_submission_source(
            source_name=source_name,
            source_bytes=source_bytes,
        )

        if verification_payload_override is None:
            if workspace is None:
                raise RuntimeError("workspace is required for verification payload collection")
            verification_payload = self._collect_verification_payload(
                artifact_verification_id=artifact_verification_id,
                workspace=workspace,
                mode=mode,
                selected_tests=selected_tests,
                settings=settings,
            )
        else:
            verification_payload = dict(verification_payload_override)
        safe_task_kind = task_plan.task_kind(
            {
                "task_kind": task_kind,
                "verification_source": verification_source,
                "compile_only": bool(compile_only),
            }
        )
        compile_only_flag = safe_task_kind == self._TASK_KIND_COMPILE_ONLY
        if compile_only_flag:
            empty = self._runtime_blob_store.put_bytes(b"").to_payload()
            verification_payload = dict(verification_payload)
            verification_payload["tests"] = [
                {
                    "name": "compile-only.in",
                    "input_file": empty,
                    "answer_name": "compile-only.ans",
                    "answer_file": empty,
                }
            ]
        return {
            "type": "verification.run",
            "run_id": run_id,
            "problem": problem,
            "username": username,
            "artifact_verification_id": artifact_verification_id,
            "mode": mode,
            "submission_path": JudgehostPayloadPreparation._normalize_text(submission_path),
            "source_name": source_name,
            "source_label": source_label,
            "source_file": source_file.to_payload(),
            "entry_point": entry_point,
            "selected_tests": list(selected_tests),
            "verification_id": verification_id,
            "verification_task_id": verification_task_id,
            "verification_program_id": verification_program_id,
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
        settings = self._configuration.snapshot()
        upload_content = self._runtime_blob_store.read(
            upload_file,
            max_bytes=settings.max_submission_source_bytes,
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
                name: source.to_payload() for name, source in extra_source_files.items()
            }
        if manual_validate_only:
            payload["manual_validate_only"] = True
        return self._prepare_execution_template_payload(payload, settings=settings)

    def _prepare_execution_template_payload(
        self,
        payload: dict[str, object],
        *,
        settings: JudgehostSettings,
    ) -> dict[str, object]:
        config_snapshot = settings.values
        source_name = decode_basename(raw=payload.get("source_name"), default="submission.cpp")
        source_file = PayloadFile.from_payload(payload["source_file"])
        source_bytes = self._runtime_blob_store.read(
            source_file,
            max_bytes=settings.max_submission_source_bytes,
        )
        if not source_bytes:
            raise RuntimeError("submission source payload is empty")
        entry_point = decode_text(raw=payload.get("entry_point"))
        extra_sources_obj = cast(dict[str, object] | None, payload.get("extra_source_files"))
        if extra_sources_obj is None:
            extra_sources_obj = {}
        extra_source_items: list[tuple[str, bytes]] = []
        for raw_name, raw_file in sorted(
            extra_sources_obj.items(),
            key=lambda item: JudgehostPayloadPreparation._normalize_text(item[0]),
        ):
            safe_name = decode_basename(raw=raw_name)
            if (not safe_name) or safe_name == source_name:
                continue
            descriptor = PayloadFile.from_payload(raw_file)
            blob = self._runtime_blob_store.read(
                descriptor,
                max_bytes=settings.max_submission_source_bytes,
            )
            if not blob:
                continue
            extra_source_items.append((safe_name, blob))
        verification_payload = cast(dict[str, object] | None, payload.get("verification_payload"))
        if verification_payload is None:
            raise RuntimeError("verification payload is required for DOMjudge compatibility")
        run_cfg_obj: dict[str, object] = {}
        run_cfg_raw = decode_text(raw=verification_payload.get("run_config_json"))
        if run_cfg_raw:
            run_cfg_obj = self._json_object(run_cfg_raw)
        problem_limits_obj = cast(
            dict[str, object] | None, verification_payload.get("problem_limits")
        )
        if problem_limits_obj is None:
            problem_limits_obj = {}
        mode = decode_text(lower=True, raw=payload.get("mode"), default="pass-fail")
        compile_only, generate_mode, main_correct = task_plan.execution_modes(payload)
        manual_validate_only = parse_bool(payload.get("manual_validate_only"), default=False)
        configured_pass_limit = max(
            1,
            parse_int(
                run_cfg_obj.get("pass_limit"),
                parse_int(problem_limits_obj.get("pass_limit"), 1),
            ),
        )
        pass_limit = configured_pass_limit
        compile_timeout = config_int(config_snapshot, "TOOLCHAIN_COMPILE_TIMEOUT_SEC")
        compile_mem_mb = config_int(config_snapshot, "TOOLCHAIN_COMPILE_MEMORY_MB")
        compile_output_limit_kb = compile_output_kb(config_snapshot)
        run_output_limit_kb = run_output_kb(config_snapshot)
        pass_bundle_max_bytes = min(
            8 * 1024 * 1024,
            max(1024, int(run_output_limit_kb * 1024 * 3 // 4)),
        )
        pass_capture_script = self._scripts.pass_capture(
            max_bytes=pass_bundle_max_bytes,
        )
        run_process_limit = config_int(config_snapshot, "RUN_EXEC_PROCESS_LIMIT")
        default_cfg = GENERAL_CONFIG_DEFAULTS
        run_tl_ms = parse_int(
            run_cfg_obj.get("time_limit_ms"),
            parse_int(
                problem_limits_obj.get("time_limit_ms"),
                parse_int(default_cfg.get("time_limit_ms", 2000), 2000),
            ),
        )
        run_mem_value = run_cfg_obj.get("memory_limit_mb")
        if run_mem_value is None:
            run_mem_value = problem_limits_obj.get("memory_limit_mb")
        if run_mem_value is None:
            run_mem_value = default_cfg.get("memory_limit_mb", 1024)
        run_mem_kb = run_memory_limit_kb(run_mem_value)
        run_tl_ms = max(100, run_tl_ms)
        run_tl_sec = max(0.1, float(run_tl_ms) / 1000.0)
        run_overshoot_sec = 0.0
        sources_files = verification_payload.get("source_files")
        sources_obj = cast(dict[str, object] | None, sources_files)
        if sources_obj is None:
            sources_obj = {}

        def _source_bytes(name: str) -> bytes:
            raw_file = sources_obj.get(name)
            if raw_file is None:
                return b""
            return self._runtime_blob_store.read(
                PayloadFile.from_payload(raw_file),
                max_bytes=settings.max_component_source_bytes,
            )

        checker_source_bytes = _source_bytes("checker.cpp")
        validator_source_bytes = _source_bytes("validator.cpp")
        interactor_source_bytes = _source_bytes("interactor.cpp")
        testlib_header_bytes = _source_bytes("testlib.h")
        if checker_source_bytes:
            checker_source_bytes = task_plan.force_cpp_define(checker_source_bytes)
        if validator_source_bytes:
            validator_source_bytes = task_plan.force_cpp_define(validator_source_bytes)
        if interactor_source_bytes:
            interactor_source_bytes = task_plan.force_cpp_define(interactor_source_bytes)
        has_interactor_payload = bool(interactor_source_bytes)
        interactive = (not compile_only) and (not generate_mode) and mode == "interactive"
        if (
            (not compile_only)
            and (not generate_mode)
            and mode == "interactive"
            and not has_interactor_payload
        ):
            raise RuntimeError("interactive mode requires interactor payload")

        compile_files: list[tuple[str, bytes, bool]] = [
            (
                "run",
                self._scripts.compile(
                    settings,
                    source_name,
                    manual_validate_only=manual_validate_only,
                    compile_only=compile_only,
                ),
                True,
            )
        ]
        if language_extensions(source_name)[0] == "java":
            compile_files.append(
                (
                    "DetectMain.java",
                    self._scripts.load("DetectMain.java").encode("utf-8"),
                    False,
                )
            )
        run_files: list[tuple[str, bytes, bool]] = []
        compare_files: list[tuple[str, bytes, bool]] = []
        if interactive:
            # DOMjudge combined run/compare wraps the provided run executable
            # itself (renames run->runjury and writes run-interactive.sh).
            # Therefore we must provide jury program as "run" here.
            if interactor_source_bytes:
                run_files.append(
                    (
                        "build",
                        self._scripts.cpp_executable_build(
                            settings,
                            "interactor.cpp", role="interactor"
                        ),
                        True,
                    )
                )
                run_files.append(("interactor.cpp", interactor_source_bytes, False))
                run_files.append(
                    (
                        "interactive.runjury",
                        self._scripts.load("interactive.runjury").encode("utf-8"),
                        True,
                    )
                )
                run_files.append(("pass-capture", pass_capture_script, True))
                if testlib_header_bytes:
                    run_files.append(("testlib.h", testlib_header_bytes, False))
            else:
                raise RuntimeError("interactive mode requires interactor payload")
            compare_files.append(
                ("run", self._scripts.compare(main_correct=main_correct), True)
            )
        else:
            run_files.append(
                (
                    "run",
                    self._scripts.run(
                        False,
                        main_correct=main_correct,
                        compile_only=compile_only,
                        generate_mode=generate_mode,
                        manual_validate_only=manual_validate_only,
                    ),
                    True,
                )
            )
            if pass_limit > 1 and not (compile_only or generate_mode):
                run_files.append(("pass-capture", pass_capture_script, True))
            if compile_only:
                compare_files.append(
                    ("run", self._scripts.compare(main_correct=False), True)
                )
            elif generate_mode:
                compare_files.append(
                    ("run", self._scripts.compare(generate_mode=True), True)
                )
                if validator_source_bytes:
                    compare_files.append(("validator.cpp", validator_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))
            else:
                compare_files.append(
                    (
                        "run",
                        self._scripts.compare(main_correct=main_correct),
                        True,
                    )
                )
                if pass_limit > 1:
                    compare_files.append(("pass-capture", pass_capture_script, True))
                if checker_source_bytes:
                    compare_files.append(("checker.cpp", checker_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))

        source_hash = submission_source_hash(source_name, source_bytes)
        if extra_source_items:
            hash_blobs: list[bytes] = [f"{source_name}\0".encode("utf-8") + source_bytes]
            hash_blobs.extend(
                f"{name}\0".encode("utf-8") + blob for name, blob in extra_source_items
            )
            source_hash = blob_set_hash(hash_blobs)
        compile_hash = executable_hash(compile_files)
        run_hash = executable_hash(run_files)
        compare_hash = executable_hash(compare_files)
        toolchain_cmd_digest = self._scripts.toolchain_cmd_digest(
            settings,
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
            "language_extensions": list(language_extensions(source_name)[1]),
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
            "language_id": language_extensions(source_name)[0],
        }
        if source_name.lower().endswith(".java") and (not entry_point):
            entry_point = self._detect_java_entry_point(source_name, source_bytes)
            run_config["entry_point"] = entry_point
        full_compile_key = compile_key(
            source_hash=source_hash,
            compile_hash=compile_hash,
            compile_config=compile_config,
            entry_point=entry_point or None,
            memory_limit=run_mem_kb,
        )
        compare_config = {
            "hash": compare_hash,
            "combined_run_compare": bool(interactive),
            "compare_args": "--validate-input" if manual_validate_only else "",
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
        verification_task_id: str = "",
        verification_program_id: str,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        bypass_case_result_cache: bool = False,
        compile_only: bool = False,
        verification_payload_override: dict[str, object] | None = None,
        payload_overrides: dict[str, object] | None = None,
        execution_template: dict[str, object] | None = None,
    ) -> dict[str, object]:
        settings = self._configuration.snapshot()
        selected = self.normalize_tests(selected_tests)
        safe_run_id = normalize_run_id(run_id)
        safe_verification_program_id = normalize_verification_program_id(
            verification_program_id
        )
        safe_verification_id = self.verification_id(verification_id)
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
            verification_task_id=str(verification_task_id or ""),
            verification_program_id=safe_verification_program_id,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            run_id=safe_run_id,
            bypass_case_result_cache=bool(bypass_case_result_cache),
            compile_only=bool(compile_only),
            verification_payload_override=verification_payload_override,
            settings=settings,
        )
        if payload_overrides is not None:
            payload.update(payload_overrides)
        payload["precomputed"] = (
            dict(execution_template)
            if execution_template is not None
            else self._prepare_execution_template_payload(payload, settings=settings)
        )
        payload["execution_signature"] = task_plan.execution_signature(payload)
        return payload
