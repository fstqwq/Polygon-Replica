from __future__ import annotations

import base64
import json
import logging
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

from app.service.judgehost.compile_spec import compile_spec
from app.service.judgehost.domjudge.cache import (
    domjudge_case_cache_ref,
    domjudge_set_hash_from_blobs,
)
from app.service.judgehost.shared import (
    _DOMJUDGE_CONTEST_ID_RE,
    domjudge_config_from_snapshot,
    domjudge_hosts_payload,
    domjudge_languages_payload,
    domjudge_lower_text,
    domjudge_path_name,
    domjudge_text,
)
from app.service.judgehost.runtime import domjudge_bool
from app.service.platform.hashing import compile_command_digest, sha256_hex_text
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.platform.runtime_cache_index import RuntimeCacheEntry, RuntimeCacheIndex

from app.service.judgehost.state import JudgehostState

logger = logging.getLogger(__name__)


class DomjudgeToolkit:
    CASE_CACHE_KIND = RuntimeCacheIndex.RESULT
    EXECUTABLE_CACHE_KIND = RuntimeCacheIndex.EXECUTABLE
    _EXECUTABLE_KINDS = frozenset({"compile", "run", "compare"})
    _EXECUTABLE_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
    _EXECUTABLE_CACHE_SIGNATURE = sha256_hex_text("domjudge-executable-cache-v1")

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

    @staticmethod
    def _cache_entry_dict(entry: RuntimeCacheEntry) -> dict[str, object]:
        return {
            "key_hash": entry.key_hash,
            "signature": entry.signature,
            "value": dict(entry.value),
            "tags": dict(entry.tags),
            "files": {
                name: {
                    "size": payload.size,
                    "sha256": payload.identity,
                    "blob_ref": payload.blob_ref,
                }
                for name, payload in entry.files.items()
            },
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
        }

    def cache_get_with_payloads(
        self,
        kind: str,
        key_hash: str,
        signature: str,
        *,
        names: list[str],
    ) -> tuple[dict[str, object], dict[str, PayloadFile]] | None:
        entry = self._s.runtime_cache_index.get(
            namespace=kind,
            key_hash=key_hash,
            signature=signature,
        )
        if entry is None:
            return None
        resolved = self._cache_entry_dict(entry)
        selected = {name: entry.files[name] for name in names if name in entry.files}
        return (resolved, selected)

    def cache_delete(self, kind: str, key_hash: str, signature: str) -> None:
        self._s.runtime_cache_index.delete(namespace=kind, key_hash=key_hash, signature=signature)

    def read_blob_ref(self, blob_ref: str, *, max_bytes: int | None = None) -> bytes | None:
        descriptor = self._s.runtime_blob_store.descriptor(blob_ref)
        if descriptor is None:
            return None
        return self._s.runtime_blob_store.read(descriptor, max_bytes=max_bytes)

    def set_hash_from_blobs(self, blobs: list[bytes]) -> str:
        return domjudge_set_hash_from_blobs(blobs)

    def store_executable_cache(
        self,
        *,
        kind: str,
        executable_hash: str,
        files: list[tuple[str, bytes, bool]],
    ) -> dict[str, PayloadFile]:
        safe_kind, safe_hash = self._executable_cache_identity(kind, executable_hash)
        file_payloads: dict[str, bytes] = {}
        manifest: list[dict[str, object]] = []
        for name, content, is_executable in sorted(files, key=lambda item: item[0]):
            safe_name = domjudge_path_name(name)
            if not safe_name or safe_name in file_payloads:
                raise RuntimeError("invalid executable cache file set")
            file_payloads[safe_name] = bytes(content)
            manifest.append({"filename": safe_name, "is_executable": bool(is_executable)})
        self._s.runtime_cache_index.put(
            namespace=self.EXECUTABLE_CACHE_KIND,
            key_hash=self._executable_cache_key_hash(safe_kind, safe_hash),
            signature=self._EXECUTABLE_CACHE_SIGNATURE,
            value={
                "schema": "domjudge-executable-cache-v1",
                "kind": safe_kind,
                "executable_hash": safe_hash,
                "files": manifest,
            },
            files=file_payloads,
            tags={"artifact_kind": "domjudge-executable", "executable_kind": safe_kind},
        )

    def read_executable_cache(
        self,
        *,
        kind: str,
        executable_hash: str,
    ) -> list[dict[str, object]] | None:
        safe_kind, safe_hash = self._executable_cache_identity(kind, executable_hash)
        entry = self._s.runtime_cache_index.get(
            namespace=self.EXECUTABLE_CACHE_KIND,
            key_hash=self._executable_cache_key_hash(safe_kind, safe_hash),
            signature=self._EXECUTABLE_CACHE_SIGNATURE,
        )
        if entry is None:
            return None
        value = dict(entry.value)
        if (
            value.get("schema") != "domjudge-executable-cache-v1"
            or value.get("kind") != safe_kind
            or value.get("executable_hash") != safe_hash
        ):
            return None
        manifest = value.get("files")
        if not isinstance(manifest, list):
            return None
        rows: list[dict[str, object]] = []
        for raw_row in manifest:
            if not isinstance(raw_row, dict):
                return None
            filename = domjudge_path_name(raw_row.get("filename"))
            if not filename or filename not in entry.files:
                return None
            rows.append(
                {
                    "filename": filename,
                    "payload": entry.files[filename],
                    "is_executable": bool(raw_row.get("is_executable")),
                }
            )
        if len(rows) != len(entry.files):
            return None
        return rows

    @classmethod
    def _executable_cache_identity(cls, kind: str, executable_hash: str) -> tuple[str, str]:
        safe_kind = domjudge_lower_text(kind)
        safe_hash = domjudge_lower_text(executable_hash)
        if safe_kind not in cls._EXECUTABLE_KINDS or cls._EXECUTABLE_HASH_RE.fullmatch(safe_hash) is None:
            raise RuntimeError("invalid executable cache identity")
        return (safe_kind, safe_hash)

    @staticmethod
    def _executable_cache_key_hash(kind: str, executable_hash: str) -> str:
        return sha256_hex_text(f"{kind}\0{executable_hash}")

    def resolve_artifact_blob(self, token: str) -> bytes | None:
        return self.read_blob_ref(token)

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
        result_json: str,
        files: Mapping[str, bytes | PayloadFile],
        shortcut_eligible: bool,
    ) -> dict[str, PayloadFile]:
        entry = self._s.runtime_cache_index.put(
            namespace=self.CASE_CACHE_KIND,
            key_hash=key_parts["key_hash"],
            signature=key_parts["signature"],
            value={
                "runresult": domjudge_lower_text(runresult),
                "runtime_sec": float(max(0.0, runtime_sec)),
                "cpu_sec": float(max(0.0, cpu_sec)),
                "wall_sec": float(max(0.0, wall_sec)),
                "memory_kb": int(max(0, memory_kb)),
                "score_text": domjudge_text(score_text),
                "result_json": result_json,
                "shortcut_eligible": bool(shortcut_eligible),
            },
            files=files,
            tags=tags,
        )
        return dict(entry.files)

    @staticmethod
    def force_cpp_define(source_bytes: bytes) -> bytes:
        payload = source_bytes
        if not payload:
            return b""
        if b"#define DOMJUDGE" in payload or b"# define DOMJUDGE" in payload:
            return payload
        return b"#ifndef DOMJUDGE\n#define DOMJUDGE 1\n#endif\n" + payload

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

    def toolchain_cmd_digest(self, source_name: str, *, manual_validate_only: bool = False) -> str:
        if manual_validate_only:
            return compile_command_digest("skip.compile", [])
        language, _exts = self.language_extensions(source_name)
        spec = compile_spec(self._s.config_values, language)
        return compile_command_digest(spec.command, spec.digest_arguments)

    def public_compile_specs(self) -> list[dict[str, object]]:
        specs = (
            compile_spec(self._s.config_values, language)
            for language in ("c", "cpp", "java", "py")
        )
        return [
            {
                "language_id": spec.language_id,
                "command": spec.command,
                "arguments": list(spec.public_arguments),
            }
            for spec in specs
        ]

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
        spec = compile_spec(self._s.config_values, language)
        command = " ".join(
            shlex.quote(token) for token in (spec.command, *spec.command_arguments)
        )
        if spec.family == "native":
            before_source = " ".join(shlex.quote(token) for token in spec.fixed_arguments)
            after_output = " ".join(shlex.quote(token) for token in spec.trailing_arguments)
            script_name = "native.compile-only" if compile_only else "native.compile"
            values = {
                "NATIVE_COMPILE_CMD": command,
                "NATIVE_BEFORE_SOURCE": before_source,
                "NATIVE_AFTER_OUTPUT": after_output,
            }
        elif spec.family == "java":
            script_name = "java.compile-only" if compile_only else "java.compile"
            values = {"JAVA_COMPILE_CMD": command}
        else:
            script_name = "python.compile-only" if compile_only else "python.compile"
            values = {
                "PYTHON_COMPILE_FLAG_SUFFIX": "".join(
                    f" {shlex.quote(token)}" for token in spec.command_arguments
                ),
            }
        template = self.load_script_asset(script_name)
        rendered = self.render_script_template(template, values)
        return rendered.encode("utf-8")

    def cpp_executable_build_script(self, source_name: str, *, role: str) -> bytes:
        compiler = str(self._s.config_values.TOOLCHAIN_CPP_COMPILER)
        safe_source = shlex.quote(domjudge_path_name(source_name, default="interactor.cpp"))
        safe_role = domjudge_text(role, default="executable")
        template = self.load_script_asset(
            "cpp.interactor.build" if safe_role == "interactor" else "cpp.executable.build"
        )
        rendered = self.render_script_template(
            template,
            {
                "ROLE": safe_role,
                "CPP_EXECUTABLE_BUILD_CMD": f"{shlex.quote(compiler)} -Wall -DDOMJUDGE -O2",
                "SOURCE_NAME": safe_source,
            },
        )
        return rendered.encode("utf-8")

    def pass_capture_script(self, *, max_bytes: int) -> bytes:
        template = self.load_script_asset("pass-capture")
        rendered = self.render_script_template(
            template,
            {"BUNDLE_MAX_BYTES": str(max(1024, int(max_bytes)))},
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

    def execution_signature(self, payload: dict[str, object] | None = None) -> str:
        payload_obj = {} if payload is None else payload
        task_kind = self.task_kind(payload_obj)
        verification_source = domjudge_lower_text(payload_obj.get("verification_source"))
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
        signature_payload = {
            "task_kind": task_kind,
            "verification_source": verification_source,
            "expected_behavior": domjudge_lower_text(payload_obj.get("expected_behavior")),
            "bypass_case_result_cache": domjudge_bool(payload_obj.get("bypass_case_result_cache"), default=False),
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
            json.dumps(signature_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
        return digest

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
        return domjudge_config_from_snapshot(self._s.config_values.snapshot())

    @staticmethod
    def languages() -> list[dict[str, object]]:
        return domjudge_languages_payload()

    def list_hosts(self) -> list[dict[str, object]]:
        with self._s.state_lock:
            return domjudge_hosts_payload(self._s.hosts_state)
