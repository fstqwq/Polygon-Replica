from __future__ import annotations

import base64
import json
import logging
import re
import shlex
import uuid
from pathlib import Path

from app.db import now_iso
from app.service.judgehost.artifact import domjudge_read_artifact_blob, resolve_artifact_blob
from app.service.judgehost.domjudge.cache import (
    domjudge_cache_blob_ref,
    domjudge_case_cache_ref,
    domjudge_manifest_digest,
    domjudge_parse_cache_blob_ref,
    domjudge_set_hash_from_blobs,
)
from app.service.judgehost.shared import (
    _DOMJUDGE_CACHE_NAME_RE,
    _DOMJUDGE_CONTEST_ID_RE,
    _DOMJUDGE_PROTOCOL_TRACE_BYTES_RE,
    _DOMJUDGE_PROTOCOL_TRACE_RE,
    domjudge_config_from_constants,
    domjudge_hosts_payload,
    domjudge_languages_payload,
    domjudge_lower_text,
    domjudge_path_name,
    domjudge_text,
)
from app.service.judgehost.runtime import domjudge_bool, domjudge_parse_float, domjudge_parse_int
from app.service.platform.hashing import compile_command_digest, sha256_hex_bytes, sha256_hex_text
from app.service.platform.judge_fs_index import JudgeFsIndexService

from .state import JudgehostState

logger = logging.getLogger(__name__)


class DomjudgeToolkit:
    CASE_CACHE_KIND = JudgeFsIndexService.KIND_CASE

    def __init__(self, state: JudgehostState) -> None:
        self._s = state

    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _TASK_KIND_GENERATE_INPUT = "generate-input"
    _TASK_KIND_MAIN_CORRECT = "main-correct"
    _TASK_KIND_SOLUTION_RUN = "solution-run"
    _TASK_KIND_SET = {
        _TASK_KIND_COMPILE_ONLY,
        _TASK_KIND_GENERATE_INPUT,
        _TASK_KIND_MAIN_CORRECT,
        _TASK_KIND_SOLUTION_RUN,
    }



    @staticmethod
    def contest_id(raw: object) -> str:
        token = domjudge_text(raw)
        if not _DOMJUDGE_CONTEST_ID_RE.fullmatch(token):
            return "local"
        return token

    def work_root(self, task_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", domjudge_text(task_id)).strip("-")
        if not safe:
            safe = f"task-{uuid.uuid4().hex[:8]}"
        return (self._s.fs_manager.judgehost_runs_root / safe).resolve()

    @staticmethod
    def b64_decode(text: str | bytes | bytearray | memoryview | None) -> bytes:
        if text is None:
            return b""
        try:
            raw = text.strip()
        except AttributeError:
            try:
                raw = bytes(text).decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise RuntimeError("DOMjudge payload must be base64 ASCII text") from exc
            except TypeError as exc:
                raise RuntimeError("DOMjudge payload must be base64 text") from exc
        if not raw:
            return b""
        try:
            return base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise RuntimeError("DOMjudge payload is not valid base64") from exc

    @staticmethod
    def payload_blob_bytes(value: str | bytes | bytearray | memoryview | None) -> bytes:
        if value is None:
            return b""
        try:
            return bytes(value)
        except TypeError:
            return DomjudgeToolkit.b64_decode(value)

    def manifest_from_files(self, files: dict[str, bytes]) -> tuple[list[dict[str, object]], str]:
        rows: list[dict[str, object]] = []
        for raw_name, raw_blob in sorted(files.items(), key=lambda item: domjudge_text(item[0])):
            path = domjudge_path_name(raw_name)
            if (not path) or (_DOMJUDGE_CACHE_NAME_RE.fullmatch(path) is None):
                continue
            blob = raw_blob
            sha256_text = sha256_hex_bytes(blob)
            size_value = int(len(blob))
            mode = "0644"
            blob_key = f"{sha256_text}:{size_value}:{mode}"
            rows.append(
                {
                    "path": path,
                    "blob_key": blob_key,
                    "sha256": sha256_text,
                    "size": size_value,
                    "mode": mode,
                }
            )
        return rows, domjudge_manifest_digest(rows)

    def validate_cache_entry(
        self,
        *,
        kind: str,
        key_hash: str,
        signature: str,
        entry: dict[str, object],
    ) -> bool:
        value_map = dict(entry["value"])
        files_map = dict(entry["files"])
        manifest_raw = list(value_map["manifest"])
        manifest_rows: list[dict[str, object]] = []
        for raw in manifest_raw:
            path = domjudge_path_name(raw["path"])
            if (not path) or (_DOMJUDGE_CACHE_NAME_RE.fullmatch(path) is None):
                return False
            sha = domjudge_lower_text(raw["sha256"])
            if re.fullmatch(r"[0-9a-f]{64}", sha) is None:
                return False
            mode = domjudge_text(raw["mode"], default="0644")
            size = max(0, int(raw["size"]))
            blob_key = domjudge_text(raw["blob_key"])
            if not blob_key:
                return False
            manifest_rows.append(
                {
                    "path": path,
                    "blob_key": blob_key,
                    "sha256": sha,
                    "size": size,
                    "mode": mode,
                }
            )
        declared_digest = domjudge_lower_text(value_map.get("manifest_digest"))
        computed_digest = domjudge_manifest_digest(manifest_rows)
        if (not declared_digest) or (declared_digest != computed_digest):
            return False
        manifest_paths = {str(item["path"]) for item in manifest_rows}
        file_paths = {domjudge_path_name(name) for name in files_map.keys()}
        if manifest_paths != file_paths:
            return False
        seen_blob: dict[str, tuple[str, int]] = {}
        for row in manifest_rows:
            path = str(row["path"])
            file_meta = files_map.get(path)
            if file_meta is None:
                return False
            meta_sha = domjudge_lower_text(file_meta["sha256"])
            meta_size = max(0, int(file_meta["size"]))
            if meta_sha != str(row["sha256"]) or meta_size != int(row["size"]):
                return False
            blob_key = str(row["blob_key"])
            expected = (str(row["sha256"]), int(row["size"]))
            blob = self.cache_read_blob(
                kind=kind,
                key_hash=key_hash,
                signature=signature,
                name=path,
            )
            if blob is None:
                return False
            blob_sha = sha256_hex_bytes(blob)
            blob_size = int(len(blob))
            if blob_sha != expected[0] or blob_size != expected[1]:
                return False
            existing = seen_blob.get(blob_key)
            if existing is not None:
                if existing != (blob_sha, blob_size):
                    return False
                continue
            seen_blob[blob_key] = (blob_sha, blob_size)
        return True

    def case_cache_ref(
        self,
        *,
        source_hash: str,
        compile_hash: str,
        run_hash: str,
        compare_hash: str,
        compile_config_hash: str,
        run_config_hash: str,
        compare_config_hash: str,
        toolchain_cmd_digest: str,
        testcase_hash: str,
    ) -> tuple[str, str]:
        return domjudge_case_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compare_hash=compare_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            compare_config_hash=compare_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_hash=testcase_hash,
        )

    def cache_get(self, kind: str, key_hash: str, signature: str) -> dict[str, object] | None:
        service = self._s.judge_fs_index_service
        if service is None:
            return None
        entry = service.get(kind=kind, key_hash=key_hash, signature=signature)
        if entry is None:
            return None
        resolved = {
            "key_hash": key_hash,
            "signature": signature,
            "value": dict(entry["value"]),
            "tags": dict(entry["tags"]),
            "files": dict(entry["files"]),
            "created_at": domjudge_text(entry.get("created_at")),
            "updated_at": domjudge_text(entry.get("updated_at")),
        }
        if not self.validate_cache_entry(
            kind=kind,
            key_hash=key_hash,
            signature=signature,
            entry=resolved,
        ):
            self.cache_delete(kind=kind, key_hash=key_hash, signature=signature)
            return None
        return resolved

    def cache_put(
        self,
        kind: str,
        key_hash: str,
        signature: str,
        value: dict[str, object],
        *,
        files: dict[str, bytes] | None = None,
        tags: dict[str, object] | None = None,
    ) -> str:
        service = self._s.judge_fs_index_service
        if service is None:
            return ""
        service.put(
            kind=kind,
            key_hash=key_hash,
            signature=signature,
            value=value,
            files=files,
            tags=tags,
        )
        return signature

    def cache_delete(self, kind: str, key_hash: str, signature: str) -> None:
        service = self._s.judge_fs_index_service
        if service is None:
            return
        service.delete(kind=kind, key_hash=key_hash, signature=signature)

    def cache_read_blob(self, kind: str, key_hash: str, signature: str, name: str) -> bytes | None:
        service = self._s.judge_fs_index_service
        if service is None:
            return None
        return service.read_blob(kind=kind, key_hash=key_hash, signature=signature, name=name)

    @staticmethod
    def cache_blob_ref(*, kind: str, key_hash: str, signature: str, name: str) -> str:
        return domjudge_cache_blob_ref(kind=kind, key_hash=key_hash, signature=signature, name=name)

    def build_cached_case(
        self,
        *,
        cache_kind: str,
        cache_key_hash: str,
        cache_signature: str,
        cache_value: dict[str, object],
        cache_files: dict[str, object] | None = None,
    ) -> dict[str, object]:
        mapping = {
            "program.out": "output_run_rel",
            "program.err": "output_error_rel",
            "system.out": "output_system_rel",
            "judgemessage.txt": "output_diff_rel",
            "program.meta": "metadata_rel",
            "compare.meta": "compare_metadata_rel",
            "teammessage.txt": "team_message_rel",
        }
        rel_map: dict[str, str] = {}
        files_map = {} if cache_files is None else cache_files
        for blob_name, rel_key in mapping.items():
            if blob_name not in files_map:
                continue
            rel_map[rel_key] = domjudge_cache_blob_ref(
                kind=cache_kind,
                key_hash=cache_key_hash,
                signature=cache_signature,
                name=blob_name,
            )
        return {
            "runresult": domjudge_lower_text(cache_value.get("runresult"), default="correct"),
            "runtime_sec": domjudge_parse_float(cache_value.get("runtime_sec"), 0.0),
            "cpu_sec": domjudge_parse_float(cache_value.get("cpu_sec"), 0.0),
            "wall_sec": domjudge_parse_float(cache_value.get("wall_sec"), 0.0),
            "memory_kb": max(0, domjudge_parse_int(cache_value.get("memory_kb"), 0)),
            "score_text": domjudge_text(cache_value.get("score_text")),
            **rel_map,
        }

    def set_hash_from_blobs(self, blobs: list[bytes]) -> str:
        return domjudge_set_hash_from_blobs(blobs)

    def read_artifact_blob(self, work_root: Path, token: str) -> bytes | None:
        return domjudge_read_artifact_blob(
            parse_cache_blob_ref=domjudge_parse_cache_blob_ref,
            cache_read_blob=lambda kind, key_hash, signature, name: self.cache_read_blob(
                kind=kind,
                key_hash=key_hash,
                signature=signature,
                name=name,
            ),
            work_root=work_root,
            token=token,
        )

    def resolve_artifact_blob(self, token: str, *, work_root: str | Path | None = None) -> bytes | None:
        return resolve_artifact_blob(
            parse_cache_blob_ref=domjudge_parse_cache_blob_ref,
            cache_read_blob=lambda kind, key_hash, signature, name: self.cache_read_blob(
                kind=kind,
                key_hash=key_hash,
                signature=signature,
                name=name,
            ),
            read_artifact_blob=self.read_artifact_blob,
            token=token,
            work_root=work_root,
        )

    def store_case_cache(
        self,
        *,
        key_parts: dict[str, str],
        tags: dict[str, object],
        runresult: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        score_text: str,
        files: dict[str, bytes],
        shortcut_eligible: bool,
    ) -> None:
        manifest_rows, manifest_digest = self.manifest_from_files(files)
        key_hash = self.cache_put(
            self.CASE_CACHE_KIND,
            key_parts["key_hash"],
            key_parts["signature"],
            {
                "runresult": domjudge_lower_text(runresult),
                "runtime_sec": float(max(0.0, runtime_sec)),
                "cpu_sec": float(max(0.0, cpu_sec)),
                "wall_sec": float(max(0.0, wall_sec)),
                "memory_kb": int(max(0, memory_kb)),
                "score_text": domjudge_text(score_text),
                "shortcut_eligible": bool(shortcut_eligible),
                "manifest": manifest_rows,
                "manifest_digest": manifest_digest,
            },
            files=files,
            tags=tags,
        )
        if not key_hash:
            return

    @staticmethod
    def strip_protocol_trace(raw: bytes) -> bytes:
        payload = raw
        if not payload:
            return b""
        if _DOMJUDGE_PROTOCOL_TRACE_BYTES_RE.search(payload) is None:
            # Most runs have no runpipe transcript markers. Avoid decoding and
            # splitting large outputs unless there is something to remove.
            return b"" if not payload.strip() else payload
        text = payload.decode("utf-8", errors="replace")
        kept: list[str] = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if _DOMJUDGE_PROTOCOL_TRACE_RE.search(line):
                continue
            kept.append(line)
        while kept and (not kept[0].strip()):
            kept.pop(0)
        while kept and (not kept[-1].strip()):
            kept.pop()
        if not kept:
            return b""
        return ("\n".join(kept) + "\n").encode("utf-8")

    @staticmethod
    def force_cpp_define(source_bytes: bytes) -> bytes:
        payload = source_bytes
        if not payload:
            return b""
        if b"#define DOMJUDGE" in payload or b"# define DOMJUDGE" in payload:
            return payload
        return b"#ifndef DOMJUDGE\n#define DOMJUDGE 1\n#endif\n" + payload

    @staticmethod
    def ensure_bytes_file(path: Path, content: bytes, *, executable: bool = False) -> None:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if executable:
            try:
                mode = int(target.stat().st_mode)
                target.chmod(mode | 0o755)
            except Exception as exc:
                logger.debug("failed to set executable bit on %s: %s", target, exc)

    def testcase_blob_ref(self, *, testcase_hash: str, signature: str, name: str) -> str:
        return self.cache_blob_ref(
            kind=self.CASE_CACHE_KIND,
            key_hash=testcase_hash,
            signature=signature,
            name=name,
        )

    def clear_testcase_registry(self) -> None:
        with self._s.testcase_registry_lock:
            self._s.testcase_registry_by_hash.clear()

    def register_cached_testcase(
        self,
        *,
        testcase_hash: str,
        in_bytes: bytes,
        ans_bytes: bytes,
    ) -> tuple[str, str]:
        safe_hash = domjudge_lower_text(testcase_hash)
        if not re.fullmatch(r"[0-9a-f]{64}", safe_hash):
            safe_hash = domjudge_set_hash_from_blobs([in_bytes, ans_bytes])
        testcase_signature = domjudge_set_hash_from_blobs([in_bytes, ans_bytes])
        if not self.cache_put(
            self.CASE_CACHE_KIND,
            safe_hash,
            testcase_signature,
            {
                "schema": "testcase-artifact.v1",
                "testcase_hash": safe_hash,
                "input_sha256": sha256_hex_bytes(in_bytes),
                "answer_sha256": sha256_hex_bytes(ans_bytes),
            },
            files={
                "input.in": in_bytes,
                "answer.ans": ans_bytes,
            },
            tags={
                "testcase_hash": safe_hash,
                "artifact_kind": "domjudge-testcase",
            },
        ):
            raise RuntimeError("judge fs index testcase store is unavailable")
        now_text = now_iso()
        input_ref = self.testcase_blob_ref(
            testcase_hash=safe_hash,
            signature=testcase_signature,
            name="input.in",
        )
        answer_ref = self.testcase_blob_ref(
            testcase_hash=safe_hash,
            signature=testcase_signature,
            name="answer.ans",
        )
        with self._s.testcase_registry_lock:
            self._s.testcase_registry_by_hash[safe_hash] = {
                "hash": safe_hash,
                "input_ref": input_ref,
                "answer_ref": answer_ref,
                "updated_at": now_text,
            }
        return (input_ref, answer_ref)

    @staticmethod
    def language_extensions(source_name: str) -> tuple[str, list[str]]:
        name = domjudge_lower_text(source_name)
        if name.endswith(".java"):
            return ("java", ["java"])
        if name.endswith(".py"):
            return ("py", ["py"])
        if name.endswith(".c"):
            return ("c", ["c"])
        return ("cpp", ["cpp", "cc", "cxx", "c++"])

    @staticmethod
    def shell_words(raw: object) -> str:
        token = domjudge_text(raw)
        if not token:
            return ""
        try:
            parts = shlex.split(token)
        except ValueError:
            parts = token.split()
        safe_parts = [shlex.quote(part) for part in parts if part]
        return " ".join(safe_parts)

    @staticmethod
    def shell_tokens(raw: object) -> list[str]:
        token = domjudge_text(raw)
        if not token:
            return []
        try:
            parts = shlex.split(token)
        except ValueError:
            parts = token.split()
        out: list[str] = []
        for part in parts:
            if part:
                out.append(part)
        return out

    def toolchain_cmd_digest(self, source_name: str, *, manual_validate_only: bool = False) -> str:
        if manual_validate_only:
            return compile_command_digest("skip.compile", [])
        language, _exts = self.language_extensions(source_name)
        if language == "java":
            command = domjudge_text(getattr(self._s.constants, "TOOLCHAIN_JAVA_COMPILER", "javac"), default="javac")
            flags = self.shell_tokens(getattr(self._s.constants, "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS", ""))
            return compile_command_digest(command, flags)
        if language == "py":
            command = "pypy3"
            flags = self.shell_tokens(getattr(self._s.constants, "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS", ""))
            return compile_command_digest(command, [*flags, "-m", "py_compile"])
        if language == "c":
            return compile_command_digest("gcc", ["-O2", "-std=gnu11", "-pipe", "-lm"])
        command = domjudge_text(getattr(self._s.constants, "TOOLCHAIN_CPP_COMPILER", "g++"), default="g++")
        flags = self.shell_tokens(
            getattr(
                self._s.constants,
                "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS",
                "-x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE",
            )
        )
        return compile_command_digest(command, flags)

    def load_script_asset(self, name: str) -> str:
        root = (Path(__file__).resolve().parent / "scripts").resolve()
        safe_name = domjudge_path_name(name)
        if safe_name != name:
            raise RuntimeError(f"invalid judgehost script asset name: {name}")
        path = (root / safe_name).resolve()
        if path.parent != root:
            raise RuntimeError(f"invalid judgehost script asset path: {name}")
        if (not path.exists()) or (not path.is_file()):
            raise RuntimeError(f"missing judgehost script asset: {safe_name}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def render_script_template(template: str, values: dict[str, str]) -> str:
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", value)
        unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", rendered)))
        if unresolved:
            raise RuntimeError(
                f"unresolved judgehost script template tokens: {', '.join(unresolved)}"
            )
        return rendered

    def compile_script(
        self,
        source_name: str,
        *,
        manual_validate_only: bool = False,
        compile_only: bool = False,
    ) -> bytes:
        if manual_validate_only:
            return self.load_script_asset("skip.compile").encode("utf-8")
        language, _exts = self.language_extensions(source_name)
        compiler = domjudge_text(getattr(self._s.constants, "TOOLCHAIN_CPP_COMPILER", "g++"), default="g++")
        java_compiler = domjudge_text(getattr(self._s.constants, "TOOLCHAIN_JAVA_COMPILER", "javac"), default="javac")
        cpp_compile_flags = self.shell_words(
            getattr(
                self._s.constants,
                "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS",
                "-x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE",
            )
        )
        java_compile_flags = self.shell_words(
            getattr(self._s.constants, "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS", "")
        )
        python_compile_flags = self.shell_words(
            getattr(self._s.constants, "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS", "")
        )
        cpp_compile_cmd = shlex.quote(compiler)
        if cpp_compile_flags:
            cpp_compile_cmd += f" {cpp_compile_flags}"
        java_compile_cmd = shlex.quote(java_compiler)
        if java_compile_flags:
            java_compile_cmd += f" {java_compile_flags}"
        python_compile_flag_suffix = f" {python_compile_flags}" if python_compile_flags else ""
        script_name = "cpp.compile-only" if compile_only else "cpp.compile"
        values = {"CPP_COMPILE_CMD": cpp_compile_cmd}
        if language == "java":
            script_name = "java.compile-only" if compile_only else "java.compile"
            values = {"JAVA_COMPILE_CMD": java_compile_cmd}
        elif language == "py":
            script_name = "python.compile-only" if compile_only else "python.compile"
            values = {
                "PYTHON_COMPILE_FLAG_SUFFIX": python_compile_flag_suffix,
            }
        template = self.load_script_asset(script_name)
        rendered = self.render_script_template(template, values)
        return rendered.encode("utf-8")

    def cpp_executable_build_script(self, source_name: str, *, role: str) -> bytes:
        compiler = domjudge_text(getattr(self._s.constants, "TOOLCHAIN_CPP_COMPILER", "g++"), default="g++")
        safe_source = shlex.quote(domjudge_path_name(source_name, default="interactor.cpp"))
        safe_role = domjudge_text(role, default="executable")
        template = self.load_script_asset("cpp.executable.build")
        rendered = self.render_script_template(
            template,
            {
                "ROLE": safe_role,
                "CPP_EXECUTABLE_BUILD_CMD": f"{shlex.quote(compiler)} -Wall -DDOMJUDGE -O2",
                "SOURCE_NAME": safe_source,
            },
        )
        return rendered.encode("utf-8")

    def task_kind(
        self,
        payload: dict[str, object] | None = None,
        *,
        verification_source: str | None = None,
        compile_only: object | None = None,
    ) -> str:
        payload_obj = {} if payload is None else payload
        explicit = domjudge_lower_text(payload_obj.get("task_kind"))
        if explicit == "generate":
            explicit = self._TASK_KIND_GENERATE_INPUT
        if explicit in self._TASK_KIND_SET:
            return explicit
        source = str(
            verification_source
            if verification_source is not None
            else (
                str(payload_obj.get("verification_source"))
                if payload_obj.get("verification_source") is not None
                else ""
            )
        ).strip().lower()
        compile_only_flag = domjudge_bool(
            compile_only if compile_only is not None else payload_obj.get("compile_only"),
            default=False,
        )
        if compile_only_flag:
            return self._TASK_KIND_COMPILE_ONLY
        if source == self._TASK_KIND_GENERATE_INPUT or source.endswith(f".{self._TASK_KIND_GENERATE_INPUT}"):
            return self._TASK_KIND_GENERATE_INPUT
        if source == self._TASK_KIND_MAIN_CORRECT or source.endswith(f".{self._TASK_KIND_MAIN_CORRECT}"):
            return self._TASK_KIND_MAIN_CORRECT
        return self._TASK_KIND_SOLUTION_RUN

    def group_key(self, payload: dict[str, object] | None = None) -> str:
        payload_obj = {} if payload is None else payload
        task_kind = self.task_kind(payload_obj)
        verification_source = domjudge_lower_text(payload_obj.get("verification_source"))
        if verification_source not in {
            self._TASK_KIND_GENERATE_INPUT,
            self._TASK_KIND_MAIN_CORRECT,
            self._TASK_KIND_SOLUTION_RUN,
        }:
            return ""
        if task_kind not in {
            self._TASK_KIND_GENERATE_INPUT,
            self._TASK_KIND_MAIN_CORRECT,
            self._TASK_KIND_SOLUTION_RUN,
        }:
            return ""
        precomputed_raw = payload_obj.get("domjudge_precomputed")
        if not isinstance(precomputed_raw, dict):
            return ""
        compile_hash = domjudge_lower_text(precomputed_raw.get("compile_hash"))
        run_hash = domjudge_lower_text(precomputed_raw.get("run_hash"))
        compare_hash = domjudge_lower_text(precomputed_raw.get("compare_hash"))
        source_hash = domjudge_lower_text(precomputed_raw.get("source_hash"))
        if (not compile_hash) or (not run_hash) or (not compare_hash) or (not source_hash):
            return ""
        compile_config = precomputed_raw.get("compile_config") or {}
        run_config = precomputed_raw.get("run_config") or {}
        compare_config = precomputed_raw.get("compare_config") or {}
        compile_config_hash = sha256_hex_text(
            json.dumps(compile_config, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
        run_config_hash = sha256_hex_text(
            json.dumps(run_config, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
        compare_config_hash = sha256_hex_text(
            json.dumps(compare_config, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
        group_payload = {
            "task_kind": task_kind,
            "verification_source": verification_source,
            "expected_behavior": domjudge_lower_text(payload_obj.get("expected_behavior")),
            "force_recompile": domjudge_bool(payload_obj.get("force_recompile"), default=False),
            "source_hash": source_hash,
            "compile_hash": compile_hash,
            "run_hash": run_hash,
            "compare_hash": compare_hash,
            "compile_config_hash": compile_config_hash,
            "run_config_hash": run_config_hash,
            "compare_config_hash": compare_config_hash,
            "toolchain_cmd_digest": domjudge_lower_text(
                compile_config.get("toolchain_cmd_digest") if isinstance(compile_config, dict) else ""
            ),
        }
        digest = sha256_hex_text(
            json.dumps(group_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
        return f"jg-{digest[:32]}"

    def is_grouped_verification_task(self, payload: dict[str, object] | None = None) -> bool:
        return bool(self.group_key(payload))

    def execution_modes(
        self,
        payload: dict[str, object] | None = None,
        *,
        verification_source: str | None = None,
        compile_only: object | None = None,
    ) -> tuple[bool, bool, bool]:
        payload_obj = {} if payload is None else payload
        kind = self.task_kind(
            payload_obj,
            verification_source=verification_source,
            compile_only=compile_only,
        )
        return (
            kind == self._TASK_KIND_COMPILE_ONLY,
            kind == self._TASK_KIND_GENERATE_INPUT,
            kind == self._TASK_KIND_MAIN_CORRECT,
        )

    def run_script(
        self,
        interactive: bool,
        *,
        main_correct: bool = False,
        compile_only: bool = False,
        generate_mode: bool = False,
        manual_validate_only: bool = False,
    ) -> bytes:
        _ = main_correct
        if interactive:
            return self.load_script_asset("interactive.run").encode("utf-8")
        if compile_only or manual_validate_only:
            script_name = "skip.run"
        elif generate_mode:
            script_name = "generate.run"
        else:
            script_name = "normal.run"
        return self.load_script_asset(script_name).encode("utf-8")

    def compare_script(
        self,
        *,
        main_correct: bool = False,
        generate_mode: bool = False,
    ) -> bytes:
        if main_correct:
            script_name = "main.compare"
        elif generate_mode:
            script_name = "generate.compare"
        else:
            script_name = "normal.compare"
        return self.load_script_asset(script_name).encode("utf-8")

    def config(self) -> dict[str, object]:
        return domjudge_config_from_constants(self._s.constants)

    @staticmethod
    def languages() -> list[dict[str, object]]:
        return domjudge_languages_payload()

    def list_hosts(self) -> list[dict[str, object]]:
        with self._s.state_lock:
            return domjudge_hosts_payload(self._s.hosts_state)
