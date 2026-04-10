from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import cast
from pathlib import Path

from app.db import now_iso
from app.service.judgehost.domjudge.cache import domjudge_source_hash
from app.service.judgehost.internal.shared import _RUN_ID_RE, _VERIFICATION_ID_RE, domjudge_lower_text, domjudge_path_name, domjudge_text
from app.service.judgehost.runtime import domjudge_bool, domjudge_parse_int
from app.service.platform.hashing import domjudge_executable_hash
from app.service.run.runtime import RUN_TEST_NAME_RE
from app.service.platform.testlib_source import workspace_testlib_header


class JudgehostEnqueueMixin:
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
        text = JudgehostEnqueueMixin._normalize_text(value)
        return text if text else default

    @staticmethod
    def _normalize_status(value: object) -> str:
        return JudgehostEnqueueMixin._normalize_text(value).lower()

    def _verification_artifact_ref(self, verification_id: str, test_name: str, ref_key: str) -> str:
        safe_verification_id = JudgehostEnqueueMixin._normalize_text(verification_id)
        safe_test_name = JudgehostEnqueueMixin._normalize_text(test_name)
        safe_ref_key = JudgehostEnqueueMixin._normalize_text(ref_key)
        if (not safe_verification_id) or (not safe_test_name) or safe_ref_key not in {"input_ref", "answer_ref"}:
            return ""
        row = self.db.fetch_one(
            f"""
            SELECT {safe_ref_key}
            FROM verification_artifact_refs
            WHERE verification_id=? AND test_name=?
            """,
            [safe_verification_id, safe_test_name],
        )
        if row is None:
            return ""
        return JudgehostEnqueueMixin._normalize_text(row[safe_ref_key])

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
            token = JudgehostEnqueueMixin._normalize_text(raw)
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
    def _payload_verification_tests(payload: dict[str, object]) -> list[dict[str, object]]:
        verification_payload = cast(dict[str, object] | None, payload.get("verification_payload"))
        if verification_payload is None:
            return []
        raw_tests = cast(list[dict[str, object]] | None, verification_payload.get("tests"))
        return [] if raw_tests is None else [dict(item) for item in raw_tests]

    @staticmethod
    def _payload_test_names(payload: dict[str, object]) -> list[str]:
        names: list[str] = []
        for item in JudgehostEnqueueMixin._payload_verification_tests(payload):
            test_name = domjudge_text(item.get("name"))
            if (not test_name) or (test_name in names):
                continue
            names.append(test_name)
        return names

    def _merge_existing_task_payload(
        self,
        *,
        task_id: str,
        payload: dict[str, object],
        reactivated: bool,
    ) -> None:
        with self._state_lock:
            row = self._tasks_by_id.get(task_id)
            if row is None:
                return
            existing_payload = cast(dict[str, object], row.get("payload") or {})
            merged_payload = dict(existing_payload)
            merged_tests = JudgehostEnqueueMixin._payload_verification_tests(existing_payload)
            seen_names = {domjudge_text(item.get("name")) for item in merged_tests}
            for item in JudgehostEnqueueMixin._payload_verification_tests(payload):
                test_name = domjudge_text(item.get("name"))
                if (not test_name) or (test_name in seen_names):
                    continue
                merged_tests.append(dict(item))
                seen_names.add(test_name)
            verification_payload = cast(dict[str, object] | None, merged_payload.get("verification_payload"))
            if verification_payload is None:
                verification_payload = {}
            verification_payload = dict(verification_payload)
            verification_payload["tests"] = merged_tests
            merged_payload["verification_payload"] = verification_payload
            selected_tests = self._normalize_list(
                cast(list[str] | None, existing_payload.get("selected_tests")),
                matcher=RUN_TEST_NAME_RE,
            )
            for test_name in JudgehostEnqueueMixin._payload_test_names(payload):
                if test_name not in selected_tests:
                    selected_tests.append(test_name)
            merged_payload["selected_tests"] = selected_tests
            row["payload"] = merged_payload
            summary = cast(dict[str, object], row.get("summary") or {})
            merged_summary = dict(summary)
            merged_summary["selected_tests"] = list(selected_tests)
            merged_summary["selected_tests_count"] = len(selected_tests)
            row["summary"] = merged_summary
            row["updated_at"] = now_iso()
            if reactivated:
                row["status"] = self.STATUS_QUEUED
                row["lease_owner"] = ""
                row["lease_expires_at"] = ""
                row["completed_at"] = ""
                row["error_text"] = ""
                row["result"] = {}

    def _append_cases_to_existing_task(
        self,
        *,
        task_id: str,
        payload: dict[str, object],
    ) -> bool:
        compile_only = self._domjudge_task_kind(payload) == self._TASK_KIND_COMPILE_ONLY
        prepared = self._domjudge_prepare_payload(payload, compile_only=compile_only)
        run_id = domjudge_text(payload.get("run_id"))
        if not run_id:
            raise RuntimeError("run id is required for judgehost enqueue")
        case_rows = self._domjudge_case_rows(
            task_id=task_id,
            run_id=run_id,
            tests_rows=prepared["tests_rows"],
            main_correct=prepared["main_correct"],
        )
        requested_test_names = [str(case_row["test_name"] or "") for case_row in case_rows if str(case_row["test_name"] or "")]
        if self._judgehost_state_store.job_for_task(task_id) is None:
            self._merge_existing_task_payload(
                task_id=task_id,
                payload=payload,
                reactivated=False,
            )
            return True
        append_result = self._judgehost_state_store.append_cases_to_task(
            task_id=task_id,
            run_id=run_id,
            case_rows=case_rows,
            now_text=now_iso(),
        )
        inserted = int(append_result.get("inserted") or 0)
        reactivated = bool(append_result.get("reactivated"))
        if inserted <= 0:
            missing_names = [
                test_name
                for test_name in requested_test_names
                if self._judgehost_state_store.case_for_task(task_id, test_name) is None
            ]
            if missing_names:
                raise RuntimeError(f"shared judgehost job append failed for {', '.join(missing_names)}")
        if reactivated:
            self._restore_existing_task_work_root(task_id=task_id, payload=payload)
        self._merge_existing_task_payload(
            task_id=task_id,
            payload=payload,
            reactivated=reactivated,
        )
        return True

    def _restore_existing_task_work_root(
        self,
        *,
        task_id: str,
        payload: dict[str, object],
    ) -> None:
        compile_only = self._domjudge_task_kind(payload) == self._TASK_KIND_COMPILE_ONLY
        prepared = self._domjudge_prepare_payload(payload, compile_only=compile_only)
        work_root = self._domjudge_work_root(task_id)
        source_dir = (work_root / "source").resolve()
        scripts_compile_dir = (work_root / "scripts" / "compile").resolve()
        scripts_run_dir = (work_root / "scripts" / "run").resolve()
        scripts_compare_dir = (work_root / "scripts" / "compare").resolve()
        for directory in (source_dir, scripts_compile_dir, scripts_run_dir, scripts_compare_dir):
            directory.mkdir(parents=True, exist_ok=True)
        source_name = prepared["source_name"]
        source_bytes = prepared["source_bytes"]
        source_path = (source_dir / source_name).resolve()
        self._domjudge_ensure_bytes_file(source_path, source_bytes, executable=False)
        for name, blob in prepared["extra_source_items"]:
            target = (source_dir / name).resolve()
            if target == source_path:
                continue
            self._domjudge_ensure_bytes_file(target, blob, executable=False)
        for name, content, is_exec in prepared["compile_files"]:
            self._domjudge_ensure_bytes_file(scripts_compile_dir / name, content, executable=is_exec)
        for name, content, is_exec in prepared["run_files"]:
            self._domjudge_ensure_bytes_file(scripts_run_dir / name, content, executable=is_exec)
        for name, content, is_exec in prepared["compare_files"]:
            self._domjudge_ensure_bytes_file(scripts_compare_dir / name, content, executable=is_exec)

    @staticmethod
    def _verification_id(run_id: str, verification_id: str) -> str:
        token = JudgehostEnqueueMixin._normalize_text(verification_id)
        if _VERIFICATION_ID_RE.fullmatch(token):
            return token
        return f"ver-{JudgehostEnqueueMixin._normalize_text(run_id)}"

    def _collect_verification_payload(
        self,
        *,
        problem: str,
        artifact_verification_id: str,
        workspace: Path,
        mode: str,
        selected_tests: list[str],
    ) -> dict[str, object]:
        if not self._include_build_payload:
            return {}
        safe_verification_id = JudgehostEnqueueMixin._normalize_text(artifact_verification_id)
        if not safe_verification_id:
            return {}

        wanted_tests: list[str] = []
        if selected_tests:
            for raw in selected_tests:
                token = Path(JudgehostEnqueueMixin._normalize_text(raw)).name
                if not RUN_TEST_NAME_RE.fullmatch(token):
                    continue
                if token in wanted_tests:
                    continue
                wanted_tests.append(token)
        else:
            selected_test_rows = self.db.fetch_all(
                """
                SELECT test_name
                FROM verification_selected_tests
                WHERE verification_id=?
                ORDER BY ordinal ASC
                """,
                [safe_verification_id],
            )
            for row in selected_test_rows:
                token = Path(JudgehostEnqueueMixin._normalize_text(row["test_name"])).name
                if not RUN_TEST_NAME_RE.fullmatch(token):
                    continue
                if token in wanted_tests:
                    continue
                wanted_tests.append(token)
                if len(wanted_tests) >= self._max_tests_per_task:
                    break

        tests_payload: list[dict[str, object]] = []
        for test_name in wanted_tests:
            input_ref = JudgehostEnqueueMixin._normalize_text(
                self._verification_artifact_ref(safe_verification_id, test_name, "input_ref")
            )
            if not input_ref:
                continue
            test_bytes = self.resolve_artifact_blob(input_ref)
            if test_bytes is None:
                continue
            ans_name = f"{Path(test_name).stem}.ans"
            answer_ref = JudgehostEnqueueMixin._normalize_text(
                self._verification_artifact_ref(safe_verification_id, test_name, "answer_ref")
            )
            ans_bytes = b""
            if answer_ref:
                resolved_answer = self.resolve_artifact_blob(answer_ref)
                if resolved_answer is not None:
                    ans_bytes = resolved_answer
            tests_payload.append(
                {
                    "name": test_name,
                    "input_b64": base64.b64encode(test_bytes).decode("ascii"),
                    "answer_name": ans_name,
                    "answer_b64": base64.b64encode(ans_bytes).decode("ascii"),
                }
            )

        workspace_resolved = workspace.resolve()

        run_config_text = ""
        run_cfg_obj: dict[str, object] = {}
        verification_row = self.db.fetch_one(
            "SELECT run_config_json FROM verifications WHERE id=?",
            [safe_verification_id],
        )
        if verification_row is not None:
            run_config_text = JudgehostEnqueueMixin._normalize_text(verification_row["run_config_json"])
        if run_config_text:
            run_cfg_obj = self._json_object(run_config_text)

        binaries: dict[str, str] = {}

        def _safe_workspace_rel_file(rel_path: str) -> Path | None:
            token = JudgehostEnqueueMixin._normalize_text(rel_path).replace("\\", "/")
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

        interactive_mode = JudgehostEnqueueMixin._normalize_status(mode) == "interactive"
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

        sources_payload: dict[str, str] = {}
        for name, source_path in source_files.items():
            blob = self._safe_read_bytes(
                source_path,
                max_bytes=self._max_binary_payload_bytes,
                label=f"{name} payload",
            )
            sources_payload[name] = base64.b64encode(blob).decode("ascii")

        return {
            "tests": tests_payload,
            "run_config_json": run_config_text,
            "problem_limits": {
                "time_limit_ms": int(problem_time_limit_ms),
                "memory_limit_mb": int(problem_memory_limit_mb),
                "pass_limit": domjudge_parse_int(problem_cfg_obj.get("pass_limit"), 1),
            },
            "binaries_b64": binaries,
            "sources_b64": sources_payload,
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
        upload_filename: str | None,
        selected_tests: list[str],
        verification_id: str,
        verification_run_ids: list[str],
        expected_behavior: str,
        verification_source: str,
        run_id: str,
        task_kind: str = "",
        force_recompile: bool = False,
        compile_only: bool = False,
    ) -> dict[str, object]:
        ctx = self._workspace_service.workspace_context(problem, username, include_recent=False)
        workspace = Path(ctx["workspace"]["path"])

        source_bytes: bytes
        source_name: str
        source_label: str
        if upload_content is not None:
            source_bytes = upload_content
            source_name = JudgehostEnqueueMixin._normalize_text_with_default(upload_filename, default="submission.cpp")
            source_label = source_name
        else:
            source_path = self._safe_workspace_source(workspace, JudgehostEnqueueMixin._normalize_text(submission_path))
            source_bytes = self._safe_read_bytes(
                source_path,
                max_bytes=self._max_source_bytes,
                label="submission payload",
            )
            source_name = source_path.name
            source_label = JudgehostEnqueueMixin._normalize_text(submission_path) or source_name
        source_name, entry_point = self._normalize_submission_source(
            source_name=source_name,
            source_bytes=source_bytes,
        )

        verification_payload = self._collect_verification_payload(
            problem=problem,
            artifact_verification_id=artifact_verification_id,
            workspace=workspace,
            mode=mode,
            selected_tests=selected_tests,
        )
        safe_task_kind = self._domjudge_task_kind(
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
            "submission_path": JudgehostEnqueueMixin._normalize_text(submission_path),
            "source_name": source_name,
            "source_label": source_label,
            "source_b64": base64.b64encode(source_bytes).decode("ascii"),
            "entry_point": entry_point,
            "selected_tests": list(selected_tests),
            "verification_id": verification_id,
            "verification_run_ids": list(verification_run_ids),
            "expected_behavior": expected_behavior,
            "verification_source": verification_source,
            "task_kind": safe_task_kind,
            "force_recompile": bool(force_recompile),
            "compile_only": bool(compile_only_flag),
            "verification_payload": verification_payload,
            "enqueued_at": now_iso(),
        }

    def _domjudge_precomputed_fields_from_payload(self, payload: dict[str, object]) -> dict[str, object]:
        source_name = domjudge_path_name(payload.get("source_name"), default="submission.cpp")
        source_bytes = self._domjudge_b64_decode(payload.get("source_b64"))
        if not source_bytes:
            raise RuntimeError("submission source payload is empty")
        entry_point = domjudge_text(payload.get("entry_point"))
        extra_sources_obj = cast(dict[str, object] | None, payload.get("extra_sources_b64"))
        if extra_sources_obj is None:
            extra_sources_obj = {}
        extra_source_items: list[tuple[str, bytes]] = []
        for raw_name, raw_blob in sorted(extra_sources_obj.items(), key=lambda item: JudgehostEnqueueMixin._normalize_text(item[0])):
            safe_name = domjudge_path_name(raw_name)
            if (not safe_name) or safe_name == source_name:
                continue
            blob = self._domjudge_b64_decode(raw_blob)
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
        compile_only, generate_mode, main_correct = self._domjudge_execution_modes(payload)
        manual_validate_only = domjudge_bool(payload.get("manual_validate_only"), default=False)
        configured_pass_limit = max(
            1,
            domjudge_parse_int(
                run_cfg_obj.get("pass_limit"),
                domjudge_parse_int(problem_limits_obj.get("pass_limit"), 1),
            ),
        )
        pass_limit = configured_pass_limit
        compile_timeout = max(1, int(getattr(self._constants, "TOOLCHAIN_COMPILE_TIMEOUT_SEC", 120) or 120))
        compile_mem_mb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_MEMORY_MB", 2048) or 2048))
        compile_output_kb = max(64, int(getattr(self._constants, "TOOLCHAIN_COMPILE_OUTPUT_KB", 65536) or 65536))
        run_output_kb = max(64, int(getattr(self._constants, "RUN_EXEC_OUTPUT_KB", 65536) or 65536))
        run_process_limit = max(1, int(getattr(self._constants, "RUN_EXEC_PROCESS_LIMIT", 64) or 64))
        default_cfg = getattr(self._constants, "GENERAL_CONFIG_DEFAULTS", {}) or {}
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
        binaries_b64 = verification_payload.get("binaries_b64")
        binaries_obj = cast(dict[str, object] | None, binaries_b64)
        if binaries_obj is None:
            binaries_obj = {}
        checker_bytes = self._domjudge_b64_decode(binaries_obj.get("checker"))
        validator_bytes = self._domjudge_b64_decode(binaries_obj.get("validator"))
        interactor_bytes = self._domjudge_b64_decode(binaries_obj.get("interactor"))
        sources_b64 = verification_payload.get("sources_b64")
        sources_obj = cast(dict[str, object] | None, sources_b64)
        if sources_obj is None:
            sources_obj = {}
        checker_source_bytes = self._domjudge_b64_decode(sources_obj.get("checker.cpp"))
        validator_source_bytes = self._domjudge_b64_decode(sources_obj.get("validator.cpp"))
        interactor_source_bytes = self._domjudge_b64_decode(sources_obj.get("interactor.cpp"))
        testlib_header_bytes = self._domjudge_b64_decode(sources_obj.get("testlib.h"))
        if checker_source_bytes:
            checker_source_bytes = self._domjudge_force_cpp_define(checker_source_bytes)
        if validator_source_bytes:
            validator_source_bytes = self._domjudge_force_cpp_define(validator_source_bytes)
        if interactor_source_bytes:
            interactor_source_bytes = self._domjudge_force_cpp_define(interactor_source_bytes)
        if checker_source_bytes:
            checker_bytes = b""
        if validator_source_bytes:
            validator_bytes = b""
        if interactor_source_bytes:
            interactor_bytes = b""
        has_interactor_payload = bool(interactor_bytes or interactor_source_bytes)
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
                self._domjudge_compile_script(
                    source_name,
                    manual_validate_only=manual_validate_only,
                    compile_only=compile_only,
                ),
                True,
            )
        ]
        if self._domjudge_language_extensions(source_name)[0] == "java":
            compile_files.append(
                ("DetectMain.java", self._domjudge_load_script_asset("DetectMain.java").encode("utf-8"), False)
            )
        run_files: list[tuple[str, bytes, bool]] = []
        compare_files: list[tuple[str, bytes, bool]] = []
        if interactive:
            # DOMjudge combined run/compare wraps the provided run executable
            # itself (renames run->runjury and writes run-interactive.sh).
            # Therefore we must provide jury program as "run" here.
            if interactor_bytes:
                run_files.append(("run", interactor_bytes, True))
            elif interactor_source_bytes:
                run_files.append(
                    ("build", self._domjudge_cpp_executable_build_script("interactor.cpp", role="interactor"), True)
                )
                run_files.append(("interactor.cpp", interactor_source_bytes, False))
                if testlib_header_bytes:
                    run_files.append(("testlib.h", testlib_header_bytes, False))
            else:
                raise RuntimeError("interactive mode requires interactor payload")
            compare_files.append(("run", self._domjudge_compare_script(main_correct=main_correct), True))
        else:
            run_files.append(
                (
                    "run",
                    self._domjudge_run_script(
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
                compare_files.append(("run", self._domjudge_compare_script(main_correct=False), True))
            elif generate_mode:
                compare_files.append(("run", self._domjudge_compare_script(generate_mode=True), True))
                if validator_source_bytes:
                    compare_files.append(("validator.cpp", validator_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))
                elif validator_bytes:
                    compare_files.append(("validator", validator_bytes, True))
            else:
                compare_files.append(("run", self._domjudge_compare_script(main_correct=main_correct), True))
                if checker_source_bytes:
                    compare_files.append(("checker.cpp", checker_source_bytes, False))
                    if testlib_header_bytes:
                        compare_files.append(("testlib.h", testlib_header_bytes, False))
                elif checker_bytes:
                    compare_files.append(("checker", checker_bytes, True))

        source_hash = domjudge_source_hash(source_name, source_bytes)
        if extra_source_items:
            hash_blobs: list[bytes] = [f"{source_name}\0".encode("utf-8") + source_bytes]
            hash_blobs.extend(f"{name}\0".encode("utf-8") + blob for name, blob in extra_source_items)
            source_hash = self._domjudge_set_hash_from_blobs(hash_blobs)
        compile_hash = domjudge_executable_hash(compile_files)
        run_hash = domjudge_executable_hash(run_files)
        compare_hash = domjudge_executable_hash(compare_files)
        toolchain_cmd_digest = self._domjudge_toolchain_cmd_digest(
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
            "language_extensions": list(self._domjudge_language_extensions(source_name)[1]),
            "script_timelimit": compile_timeout,
            "script_memory_limit": int(compile_mem_mb * 1024),
            "script_filesize_limit": int(compile_output_kb),
        }
        run_config = {
            "hash": run_hash,
            "time_limit": run_tl_sec,
            "overshoot": run_overshoot_sec,
            "memory_limit": run_mem_kb,
            "output_limit": int(run_output_kb),
            "process_limit": run_process_limit,
            "entry_point": entry_point or None,
            "pass_limit": pass_limit,
            "language_id": self._domjudge_language_extensions(source_name)[0],
        }
        if source_name.lower().endswith(".java") and (not entry_point):
            run_config["entry_point"] = self._detect_java_entry_point(source_name, source_bytes)
        compare_config = {
            "hash": compare_hash,
            "combined_run_compare": bool(interactive),
            "compare_args": " ".join(
                [*(['--validate-input'] if manual_validate_only else []), *checker_args]
            ),
            "script_timelimit": int(compare_script_timelimit),
            "script_memory_limit": max(run_mem_kb, int(compile_mem_mb * 1024)),
            "script_filesize_limit": int(run_output_kb),
        }
        return {
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
        upload_filename: str | None,
        run_id: str,
        selected_tests: list[str] | None,
        verification_id: str,
        verification_run_ids: list[str] | None,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        force_recompile: bool = False,
        compile_only: bool = False,
    ) -> dict[str, object]:
        selected = self._normalize_list(selected_tests, matcher=RUN_TEST_NAME_RE)
        verification_run_id_list = self._normalize_list(verification_run_ids, matcher=_RUN_ID_RE)
        safe_run_id = self._normalize_run_id(run_id)
        payload = self._build_task_payload(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_filename=upload_filename,
            selected_tests=selected,
            verification_id=verification_id,
            verification_run_ids=verification_run_id_list,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            run_id=safe_run_id,
            force_recompile=bool(force_recompile),
            compile_only=bool(compile_only),
        )
        payload["domjudge_precomputed"] = self._domjudge_precomputed_fields_from_payload(payload)
        payload["domjudge_group_key"] = self._domjudge_group_key(payload)
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
        safe_task_kind = self._domjudge_task_kind(
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
            "verification_source": JudgehostEnqueueMixin._normalize_text_with_default(
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
        upload_filename: str | None,
        run_id: str | None = None,
        selected_tests: list[str] | None,
        verification_id: str = "",
        verification_run_ids: list[str] | None = None,
        expected_behavior: str,
        verification_source: str,
        task_kind: str = "",
        force_recompile: bool = False,
        compile_only: bool = False,
        persist_verification_run: bool = False,
        prepared_payload: dict[str, object] | None = None,
    ) -> str:
        safe_run_id = self._normalize_run_id(run_id if run_id else verification_id)
        safe_verification_id = JudgehostEnqueueMixin._normalize_text(
            verification_id if verification_id else safe_run_id
        )
        selected = self._normalize_list(selected_tests, matcher=RUN_TEST_NAME_RE)
        verification_run_id_list = self._normalize_list(verification_run_ids, matcher=_RUN_ID_RE)
        if not verification_run_id_list:
            verification_run_id_list = [safe_run_id]
        payload = self._build_task_payload(
            problem=problem,
            username=username,
            artifact_verification_id=artifact_verification_id,
            mode=mode,
            submission_path=submission_path,
            upload_content=upload_content,
            upload_filename=upload_filename,
            selected_tests=selected,
            verification_id=safe_verification_id,
            verification_run_ids=verification_run_id_list,
            expected_behavior=expected_behavior,
            verification_source=verification_source,
            task_kind=task_kind,
            run_id=safe_run_id,
            force_recompile=bool(force_recompile),
            compile_only=bool(compile_only),
        )
        if prepared_payload is not None:
            payload.update(dict(prepared_payload))
        payload["domjudge_precomputed"] = self._domjudge_precomputed_fields_from_payload(payload)
        payload["domjudge_group_key"] = self._domjudge_group_key(payload)
        safe_task_kind = self._domjudge_task_kind(payload)
        payload["run_id"] = safe_run_id
        payload["problem"] = problem
        payload["username"] = username
        payload["artifact_verification_id"] = artifact_verification_id
        payload["mode"] = mode
        payload["submission_path"] = JudgehostEnqueueMixin._normalize_text(submission_path)
        payload["selected_tests"] = list(selected)
        payload["verification_id"] = safe_verification_id
        payload["verification_run_ids"] = list(verification_run_id_list)
        payload["expected_behavior"] = expected_behavior
        payload["verification_source"] = verification_source
        payload["task_kind"] = safe_task_kind
        payload["force_recompile"] = bool(force_recompile)
        payload["compile_only"] = bool(safe_task_kind == self._TASK_KIND_COMPILE_ONLY)
        safe_verification_id_source = (
            self._verification_id(safe_run_id, safe_verification_id)
            if not verification_id
            else verification_id
        )
        safe_verification_id = JudgehostEnqueueMixin._normalize_text(safe_verification_id_source)
        payload["verification_id"] = safe_verification_id
        payload["run_id"] = safe_run_id
        task_id = ""
        summary: dict[str, object] | None = None
        while True:
            with self._state_lock:
                existing_task_id = (
                        JudgehostEnqueueMixin._normalize_text(existing_task_id_obj)
                        if (existing_task_id_obj := self._task_id_by_run.get(safe_run_id))
                        is not None
                        else ""
                    )
                if existing_task_id:
                    existing_task = self._tasks_by_id.get(existing_task_id)
                    if existing_task is None:
                        self._task_id_by_run.pop(safe_run_id, None)
                    else:
                        existing_status = (
                            JudgehostEnqueueMixin._normalize_status(existing_status_obj)
                            if (existing_status_obj := existing_task.get("status")) is not None
                            else ""
                        )
                        if existing_status != self.STATUS_ENQUEUING:
                            break
                if not existing_task_id or existing_task_id not in self._tasks_by_id:
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
                                cast(dict[str, object], cast(dict[str, object], payload["domjudge_precomputed"])["run_config"]).get(
                                    "pass_limit",
                                    1,
                                )
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
                    self._tasks_by_id[task_id] = {
                        "id": task_id,
                        "run_id": safe_run_id,
                        "problem_slug": str(problem),
                        "username": str(username),
                        "artifact_verification_id": str(artifact_verification_id),
                        "mode": str(mode),
                        "verification_id": safe_verification_id,
                        "run_id": safe_run_id,
                        "status": self.STATUS_ENQUEUING,
                        "payload": dict(payload),
                        "result": {},
                        "persist_verification_run": bool(persist_verification_run),
                        "error_text": "",
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "created_at": now_text,
                        "updated_at": now_text,
                        "completed_at": "",
                        "attempt_count": 0,
                        "summary": dict(summary),
                    }
                    self._task_id_by_run[safe_run_id] = task_id
                    break
            # Another thread is creating the same run task; wait for terminal enqueue step.
            time.sleep(0.01)

        if existing_task_id:
            self._append_cases_to_existing_task(
                task_id=existing_task_id,
                payload=payload,
            )
            existing_job = self._judgehost_state_store.job_for_task(existing_task_id)
            if existing_job is not None:
                self._domjudge_apply_cache_shortcuts_for_job(
                    int(existing_job["job_id"]),
                    hostname=self._normalize_hostname("prequeue-cache"),
                )
                self._domjudge_finalize_if_ready(int(existing_job["job_id"]))
            return existing_task_id

        if summary is None or not task_id:
            raise RuntimeError("failed to allocate judgehost task")

        if not self._domjudge_is_grouped_verification_task(payload):
            self._domjudge_try_prequeue_cache_finalize(
                task_id=task_id,
                run_id=safe_run_id,
                payload=dict(payload),
            )
        with self._state_lock:
            row = self._tasks_by_id.get(task_id)
            if row is not None:
                row_status_obj = row.get("status")
                row_status = JudgehostEnqueueMixin._normalize_status(row_status_obj) if row_status_obj is not None else ""
                if row_status == self.STATUS_ENQUEUING:
                    row["status"] = self.STATUS_QUEUED
                    row["updated_at"] = now_iso()
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
            verification_id=JudgehostEnqueueMixin._normalize_text(verification_id),
            verification_run_ids=list(verification_run_ids or [run_id]),
            expected_behavior=expected_behavior or "compile",
            verification_source=verification_source or "compile.only",
            task_kind=self._TASK_KIND_COMPILE_ONLY,
            compile_only=True,
            persist_verification_run=False,
            prepared_payload=None if prepared_payload is None else dict(prepared_payload),
        )
