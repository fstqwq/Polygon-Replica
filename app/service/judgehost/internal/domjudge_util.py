from __future__ import annotations

from .shared import (
    Path,
    _DOMJUDGE_CACHE_NAME_RE,
    _DOMJUDGE_CONTEST_ID_RE,
    _DOMJUDGE_PROTOCOL_TRACE_RE,
    _DOMJUDGE_PROTOCOL_TRACE_BYTES_RE,
    _DOMJUDGE_SUBMIT_ID_RE,
    base64,
    compile_command_digest,
    domjudge_bool,
    domjudge_cache_blob_ref,
    domjudge_case_cache_ref,
    domjudge_config_from_constants,
    domjudge_feedback_line_from_bytes,
    domjudge_feedback_line_from_text,
    domjudge_hosts_payload,
    domjudge_json_hash,
    domjudge_lower_text,
    domjudge_languages_payload,
    domjudge_manifest_digest,
    domjudge_parse_cache_blob_ref,
    domjudge_parse_float,
    domjudge_parse_int,
    domjudge_parse_meta_text,
    domjudge_path_name,
    domjudge_parse_script_id,
    domjudge_read_artifact_blob,
    domjudge_rewrite_untrusted_runresult,
    domjudge_script_ids,
    domjudge_script_provider_job_id,
    domjudge_set_hash_from_blobs,
    domjudge_sha256_bytes,
    domjudge_solve_output_cache_ref,
    domjudge_source_hash,
    domjudge_text,
    json,
    logger,
    now_iso,
    re,
    resolve_artifact_blob,
    sha256_hex_bytes,
    sha256_hex_text,
    shlex,
    sqlite3,
    uuid,
)


class JudgehostDomjudgeUtilsMixin:
    _TASK_KIND_COMPILE_ONLY = "compile-only"
    _TASK_KIND_GENERATE = "generate"
    _TASK_KIND_SOLVE = "solve"
    _TASK_KIND_SET = {_TASK_KIND_COMPILE_ONLY, _TASK_KIND_GENERATE, _TASK_KIND_SOLVE}

    @staticmethod
    def _domjudge_parse_float(raw: object, default: float = 0.0) -> float:
        return domjudge_parse_float(raw, default)

    @staticmethod
    def _domjudge_parse_int(raw: object, default: int = 0) -> int:
        return domjudge_parse_int(raw, default)

    def _domjudge_rewrite_untrusted_runresult(
        self,
        runresult: str,
        *,
        cpu_sec: float,
        run_cfg_obj: dict[str, object],
    ) -> str:
        return domjudge_rewrite_untrusted_runresult(
            runresult,
            cpu_sec=cpu_sec,
            run_cfg_obj=run_cfg_obj,
        )

    @staticmethod
    def _domjudge_parse_meta_text(text: str) -> dict[str, str]:
        return domjudge_parse_meta_text(text)

    @staticmethod
    def _domjudge_bool(raw: object, default: bool = False) -> bool:
        return domjudge_bool(raw, default)

    @staticmethod
    def _domjudge_feedback_line_from_text(text: str, *, max_chars: int = 240) -> str:
        return domjudge_feedback_line_from_text(text, max_chars=max_chars)

    @staticmethod
    def _domjudge_feedback_line_from_bytes(blob: bytes, *, max_chars: int = 240) -> str:
        return domjudge_feedback_line_from_bytes(blob, max_chars=max_chars)

    @staticmethod
    def _domjudge_text(raw: object, *, default: str = "") -> str:
        return domjudge_text(raw, default=default)

    @staticmethod
    def _domjudge_lower_text(raw: object, *, default: str = "") -> str:
        return domjudge_lower_text(raw, default=default)

    @staticmethod
    def _domjudge_path_name(raw: object, *, default: str = "") -> str:
        return domjudge_path_name(raw, default=default)

    @staticmethod
    def _domjudge_submit_id_from_run_id(run_id: str) -> str:
        token = domjudge_text(run_id)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", token).strip("-")
        if not safe:
            safe = f"r-{uuid.uuid4().hex[:12]}"
        if not _DOMJUDGE_SUBMIT_ID_RE.fullmatch(safe):
            safe = f"s-{uuid.uuid4().hex[:12]}"
        return safe[:64]

    @staticmethod
    def _domjudge_contest_id(raw: object) -> str:
        token = domjudge_text(raw)
        if not _DOMJUDGE_CONTEST_ID_RE.fullmatch(token):
            return "local"
        return token

    def _domjudge_work_root(self, task_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", domjudge_text(task_id)).strip("-")
        if not safe:
            safe = f"task-{uuid.uuid4().hex[:8]}"
        return (self._settings.run_root / "judgehost-domjudge" / safe).resolve()

    @staticmethod
    def _domjudge_b64_decode(text: object) -> bytes:
        if text is None:
            return b""
        if isinstance(text, str):
            raw = text.strip()
        elif isinstance(text, (bytes, bytearray, memoryview)):
            try:
                raw = bytes(text).decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise RuntimeError("DOMjudge payload must be base64 ASCII text") from exc
        else:
            raise RuntimeError("DOMjudge payload must be base64 text")
        if not raw:
            return b""
        try:
            return base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise RuntimeError("DOMjudge payload is not valid base64") from exc

    @staticmethod
    def _domjudge_payload_blob_bytes(value: object) -> bytes:
        if value is None:
            return b""
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        return JudgehostDomjudgeUtilsMixin._domjudge_b64_decode(value)

    @staticmethod
    def _domjudge_json_hash(payload: object) -> str:
        return domjudge_json_hash(payload)

    @staticmethod
    def _domjudge_source_hash(source_name: str, source_bytes: bytes) -> str:
        return domjudge_source_hash(source_name, source_bytes)

    @staticmethod
    def _domjudge_manifest_digest(rows: list[dict[str, object]]) -> str:
        return domjudge_manifest_digest(rows)

    def _domjudge_manifest_from_files(self, files: dict[str, bytes]) -> tuple[list[dict[str, object]], str]:
        rows: list[dict[str, object]] = []
        for raw_name, raw_blob in sorted(files.items(), key=lambda item: domjudge_text(item[0])):
            path = domjudge_path_name(raw_name)
            if (not path) or (_DOMJUDGE_CACHE_NAME_RE.fullmatch(path) is None):
                continue
            blob = bytes(raw_blob or b"")
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
        return rows, self._domjudge_manifest_digest(rows)

    def _domjudge_validate_cache_entry(
        self,
        *,
        kind: str,
        key_hash: str,
        signature: str,
        entry: dict[str, object],
    ) -> bool:
        value_obj = entry.get("value")
        files_obj = entry.get("files")
        value_map = value_obj if isinstance(value_obj, dict) else {}
        files_map = files_obj if isinstance(files_obj, dict) else {}
        manifest_raw = value_map.get("manifest")
        if not isinstance(manifest_raw, list):
            return False
        manifest_rows: list[dict[str, object]] = []
        for raw in manifest_raw:
            if not isinstance(raw, dict):
                return False
            path = domjudge_path_name(raw.get("path"))
            if (not path) or (_DOMJUDGE_CACHE_NAME_RE.fullmatch(path) is None):
                return False
            sha = domjudge_lower_text(raw.get("sha256"))
            if re.fullmatch(r"[0-9a-f]{64}", sha) is None:
                return False
            mode = domjudge_text(raw.get("mode"), default="0644")
            try:
                size = max(0, int(raw.get("size") or 0))
            except Exception:
                return False
            blob_key = domjudge_text(raw.get("blob_key"))
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
        computed_digest = self._domjudge_manifest_digest(manifest_rows)
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
            if not isinstance(file_meta, dict):
                return False
            meta_sha = domjudge_lower_text(file_meta.get("sha256"))
            try:
                meta_size = max(0, int(file_meta.get("size") or 0))
            except Exception:
                return False
            if meta_sha != str(row["sha256"]) or meta_size != int(row["size"]):
                return False
            blob_key = str(row["blob_key"])
            expected = (str(row["sha256"]), int(row["size"]))
            blob = self._domjudge_cache_read_blob(
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

    def _domjudge_case_cache_ref(
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

    def _domjudge_solve_output_cache_ref(
        self,
        *,
        source_hash: str,
        compile_hash: str,
        run_hash: str,
        compile_config_hash: str,
        run_config_hash: str,
        toolchain_cmd_digest: str,
        testcase_input_hash: str,
    ) -> tuple[str, str]:
        return domjudge_solve_output_cache_ref(
            source_hash=source_hash,
            compile_hash=compile_hash,
            run_hash=run_hash,
            compile_config_hash=compile_config_hash,
            run_config_hash=run_config_hash,
            toolchain_cmd_digest=toolchain_cmd_digest,
            testcase_input_hash=testcase_input_hash,
        )

    def _domjudge_cache_get(self, kind: str, key_hash: str, signature: str) -> dict[str, object] | None:
        service = self._judge_fs_index_service
        if service is None:
            return None
        entry = service.get(kind=kind, key_hash=key_hash, signature=signature)
        if not isinstance(entry, dict):
            return None
        value_obj = entry.get("value")
        tags_obj = entry.get("tags")
        files_obj = entry.get("files")
        resolved = {
            "key_hash": key_hash,
            "signature": signature,
            "value": dict(value_obj) if isinstance(value_obj, dict) else {},
            "tags": dict(tags_obj) if isinstance(tags_obj, dict) else {},
            "files": dict(files_obj) if isinstance(files_obj, dict) else {},
            "created_at": domjudge_text(entry.get("created_at")),
            "updated_at": domjudge_text(entry.get("updated_at")),
        }
        if not self._domjudge_validate_cache_entry(
            kind=kind,
            key_hash=key_hash,
            signature=signature,
            entry=resolved,
        ):
            self._domjudge_cache_delete(kind=kind, key_hash=key_hash, signature=signature)
            return None
        return resolved

    def _domjudge_cache_put(
        self,
        kind: str,
        key_hash: str,
        signature: str,
        value: dict[str, object],
        *,
        files: dict[str, bytes] | None = None,
        tags: dict[str, object] | None = None,
    ) -> str:
        service = self._judge_fs_index_service
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

    def _domjudge_cache_delete(self, kind: str, key_hash: str, signature: str) -> None:
        service = self._judge_fs_index_service
        if service is None:
            return
        service.delete(kind=kind, key_hash=key_hash, signature=signature)

    def _domjudge_cache_read_blob(self, kind: str, key_hash: str, signature: str, name: str) -> bytes | None:
        service = self._judge_fs_index_service
        if service is None:
            return None
        return service.read_blob(kind=kind, key_hash=key_hash, signature=signature, name=name)

    @staticmethod
    def _domjudge_cache_blob_ref(*, kind: str, key_hash: str, signature: str, name: str) -> str:
        return domjudge_cache_blob_ref(kind=kind, key_hash=key_hash, signature=signature, name=name)

    @staticmethod
    def _domjudge_parse_cache_blob_ref(token: str) -> tuple[str, str, str, str] | None:
        return domjudge_parse_cache_blob_ref(token)

    def _domjudge_materialize_cached_case(
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
        files_map = dict(cache_files) if isinstance(cache_files, dict) else {}
        for blob_name, rel_key in mapping.items():
            if blob_name not in files_map:
                continue
            rel_map[rel_key] = self._domjudge_cache_blob_ref(
                kind=cache_kind,
                key_hash=cache_key_hash,
                signature=cache_signature,
                name=blob_name,
            )
        return {
            "runresult": domjudge_lower_text(cache_value.get("runresult"), default="correct"),
            "runtime_sec": self._domjudge_parse_float(cache_value.get("runtime_sec"), 0.0),
            "cpu_sec": self._domjudge_parse_float(cache_value.get("cpu_sec"), 0.0),
            "wall_sec": self._domjudge_parse_float(cache_value.get("wall_sec"), 0.0),
            "memory_kb": max(0, self._domjudge_parse_int(cache_value.get("memory_kb"), 0)),
            "score_text": domjudge_text(cache_value.get("score_text")),
            **rel_map,
        }

    @staticmethod
    def _domjudge_sha256_bytes(blob: bytes) -> str:
        return domjudge_sha256_bytes(blob)

    def _domjudge_set_hash_from_blobs(self, blobs: list[bytes]) -> str:
        return domjudge_set_hash_from_blobs(blobs)

    def _domjudge_read_artifact_blob(self, work_root: Path, token: str) -> bytes | None:
        return domjudge_read_artifact_blob(
            parse_cache_blob_ref=self._domjudge_parse_cache_blob_ref,
            cache_read_blob=lambda kind, key_hash, signature, name: self._domjudge_cache_read_blob(
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
            parse_cache_blob_ref=self._domjudge_parse_cache_blob_ref,
            cache_read_blob=lambda kind, key_hash, signature, name: self._domjudge_cache_read_blob(
                kind=kind,
                key_hash=key_hash,
                signature=signature,
                name=name,
            ),
            read_artifact_blob=self._domjudge_read_artifact_blob,
            token=token,
            work_root=work_root,
        )

    def _domjudge_store_case_cache(
        self,
        *,
        key_parts: dict[str, object],
        tags: dict[str, object],
        runresult: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        score_text: str,
        files: dict[str, bytes],
    ) -> None:
        manifest_rows, manifest_digest = self._domjudge_manifest_from_files(files)
        key_hash = self._domjudge_cache_put(
            self.CASE_CACHE_KIND,
            str(key_parts.get("key_hash") or ""),
            str(key_parts.get("signature") or ""),
            {
                "runresult": domjudge_lower_text(runresult),
                "runtime_sec": float(max(0.0, runtime_sec)),
                "cpu_sec": float(max(0.0, cpu_sec)),
                "wall_sec": float(max(0.0, wall_sec)),
                "memory_kb": int(max(0, memory_kb)),
                "score_text": domjudge_text(score_text),
                "manifest": manifest_rows,
                "manifest_digest": manifest_digest,
            },
            files=files,
            tags=tags,
        )
        if not key_hash:
            return

    def _domjudge_store_solve_output_cache(
        self,
        *,
        key_parts: dict[str, object],
        tags: dict[str, object],
        output_hash: str,
        runtime_sec: float,
        cpu_sec: float,
        wall_sec: float,
        memory_kb: int,
        files: dict[str, bytes],
    ) -> None:
        manifest_rows, manifest_digest = self._domjudge_manifest_from_files(files)
        key_hash = self._domjudge_cache_put(
            self.SOLVE_OUTPUT_CACHE_KIND,
            str(key_parts.get("key_hash") or ""),
            str(key_parts.get("signature") or ""),
            {
                "output_hash": domjudge_lower_text(output_hash),
                "runtime_sec": float(max(0.0, runtime_sec)),
                "cpu_sec": float(max(0.0, cpu_sec)),
                "wall_sec": float(max(0.0, wall_sec)),
                "memory_kb": int(max(0, memory_kb)),
                "runresult": "correct",
                "manifest": manifest_rows,
                "manifest_digest": manifest_digest,
            },
            files=files,
            tags=tags,
        )
        if not key_hash:
            return

    @staticmethod
    def _domjudge_strip_protocol_trace(raw: bytes) -> bytes:
        payload = bytes(raw or b"")
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
    def _domjudge_force_cpp_define(source_bytes: bytes) -> bytes:
        payload = bytes(source_bytes or b"")
        if not payload:
            return b""
        if b"#define DOMJUDGE" in payload or b"# define DOMJUDGE" in payload:
            return payload
        return b"#ifndef DOMJUDGE\n#define DOMJUDGE 1\n#endif\n" + payload

    @staticmethod
    def _domjudge_ensure_bytes_file(path: Path, content: bytes, *, executable: bool = False) -> None:
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(content))
        if executable:
            try:
                mode = int(target.stat().st_mode)
                target.chmod(mode | 0o755)
            except Exception as exc:
                logger.debug("failed to set executable bit on %s: %s", target, exc)

    def _domjudge_testcase_cache_root(self) -> Path:
        root = (self._settings.cache_root / "judgehost-domjudge-testcases").resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _domjudge_testcase_marker_root(self) -> Path:
        root = self._domjudge_testcase_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def clear_testcase_registry(self) -> None:
        with self._testcase_registry_lock:
            self._testcase_registry_next_id = 1
            self._testcase_registry_by_hash.clear()
            self._testcase_registry_by_id.clear()

    def _domjudge_testcase_cache_paths(self, testcase_hash: str) -> tuple[Path, Path]:
        token = domjudge_lower_text(testcase_hash)
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            token = sha256_hex_text(token, errors="replace")
        case_root = (self._domjudge_testcase_cache_root() / token[:2] / token).resolve()
        return ((case_root / "input.in").resolve(), (case_root / "answer.ans").resolve())

    def _domjudge_register_cached_testcase(
        self,
        conn: sqlite3.Connection,
        *,
        testcase_hash: str,
        in_bytes: bytes,
        ans_bytes: bytes,
    ) -> tuple[int, str, str]:
        _ = conn
        safe_hash = domjudge_lower_text(testcase_hash)
        if not re.fullmatch(r"[0-9a-f]{64}", safe_hash):
            safe_hash = self._domjudge_set_hash_from_blobs([bytes(in_bytes), bytes(ans_bytes)])
        now_text = now_iso()
        in_path, ans_path = self._domjudge_testcase_cache_paths(safe_hash)
        self._domjudge_ensure_bytes_file(in_path, bytes(in_bytes), executable=False)
        self._domjudge_ensure_bytes_file(ans_path, bytes(ans_bytes), executable=False)
        with self._testcase_registry_lock:
            entry = self._testcase_registry_by_hash.get(safe_hash) or {}
            testcase_id = 0
            try:
                testcase_id = int(entry.get("id") or 0)
            except Exception:
                testcase_id = 0
            if testcase_id <= 0:
                testcase_id = max(1, int(self._testcase_registry_next_id))
                while testcase_id in self._testcase_registry_by_id:
                    testcase_id += 1
                self._testcase_registry_next_id = int(testcase_id + 1)
            record = {
                "id": int(testcase_id),
                "hash": safe_hash,
                "input_path": str(in_path),
                "answer_path": str(ans_path),
                "updated_at": now_text,
            }
            self._testcase_registry_by_hash[safe_hash] = dict(record)
            self._testcase_registry_by_id[int(testcase_id)] = dict(record)
            marker_dir = self._domjudge_testcase_marker_root()
            marker = (marker_dir / safe_hash).resolve()
            if marker.parent == marker_dir:
                marker.write_bytes(b"")
        return (int(testcase_id), str(in_path), str(ans_path))

    @staticmethod
    def _domjudge_language_extensions(source_name: str) -> tuple[str, list[str]]:
        name = domjudge_lower_text(source_name)
        if name.endswith(".java"):
            return ("java", ["java"])
        if name.endswith(".py"):
            return ("py", ["py"])
        if name.endswith(".c"):
            return ("c", ["c"])
        return ("cpp", ["cpp", "cc", "cxx", "c++"])

    @staticmethod
    def _domjudge_shell_words(raw: object) -> str:
        token = domjudge_text(raw)
        if not token:
            return ""
        try:
            parts = shlex.split(token)
        except ValueError:
            parts = token.split()
        safe_parts = [shlex.quote(str(part or "")) for part in parts if str(part or "")]
        return " ".join(safe_parts)

    @staticmethod
    def _domjudge_shell_tokens(raw: object) -> list[str]:
        token = domjudge_text(raw)
        if not token:
            return []
        try:
            parts = shlex.split(token)
        except ValueError:
            parts = token.split()
        out: list[str] = []
        for part in parts:
            token = domjudge_text(part)
            if token:
                out.append(token)
        return out

    def _domjudge_toolchain_cmd_digest(self, source_name: str) -> str:
        language, _exts = self._domjudge_language_extensions(source_name)
        if language == "java":
            command = domjudge_text(getattr(self._constants, "TOOLCHAIN_JAVA_COMPILER", "javac"), default="javac")
            flags = self._domjudge_shell_tokens(getattr(self._constants, "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS", ""))
            return compile_command_digest(command, flags)
        if language == "py":
            command = "pypy3"
            flags = self._domjudge_shell_tokens(getattr(self._constants, "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS", ""))
            return compile_command_digest(command, [*flags, "-m", "py_compile"])
        if language == "c":
            return compile_command_digest("gcc", ["-O2", "-std=gnu11", "-pipe", "-lm"])
        command = domjudge_text(getattr(self._constants, "TOOLCHAIN_CPP_COMPILER", "g++"), default="g++")
        flags = self._domjudge_shell_tokens(
            getattr(
                self._constants,
                "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS",
                "-x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE",
            )
        )
        return compile_command_digest(command, flags)

    @staticmethod
    def _domjudge_script_assets_root() -> Path:
        return (Path(__file__).resolve().parents[1] / "scripts").resolve()

    def _domjudge_load_script_asset(self, name: str) -> str:
        root = self._domjudge_script_assets_root()
        safe_name = domjudge_path_name(name)
        if safe_name != domjudge_text(name):
            raise RuntimeError(f"invalid judgehost script asset name: {name}")
        path = (root / safe_name).resolve()
        if path.parent != root:
            raise RuntimeError(f"invalid judgehost script asset path: {name}")
        if (not path.exists()) or (not path.is_file()):
            raise RuntimeError(f"missing judgehost script asset: {safe_name}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _domjudge_render_script_template(template: str, values: dict[str, str]) -> str:
        rendered = str(template or "")
        for key, value in values.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
        unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", rendered)))
        if unresolved:
            joined = ", ".join(unresolved)
            raise RuntimeError(f"unresolved judgehost script template tokens: {joined}")
        return rendered

    def _domjudge_compile_script(self, source_name: str) -> bytes:
        language, _exts = self._domjudge_language_extensions(source_name)
        compiler = domjudge_text(getattr(self._constants, "TOOLCHAIN_CPP_COMPILER", "g++"), default="g++")
        java_compiler = domjudge_text(getattr(self._constants, "TOOLCHAIN_JAVA_COMPILER", "javac"), default="javac")
        cpp_compile_flags = self._domjudge_shell_words(
            getattr(
                self._constants,
                "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS",
                "-x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE",
            )
        )
        java_compile_flags = self._domjudge_shell_words(
            getattr(self._constants, "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS", "")
        )
        python_compile_flags = self._domjudge_shell_words(
            getattr(self._constants, "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS", "")
        )
        cpp_compile_cmd = shlex.quote(compiler)
        if cpp_compile_flags:
            cpp_compile_cmd += f" {cpp_compile_flags}"
        java_compile_cmd = shlex.quote(java_compiler)
        if java_compile_flags:
            java_compile_cmd += f" {java_compile_flags}"
        python_compile_flag_suffix = f" {python_compile_flags}" if python_compile_flags else ""
        script_name = "cpp.compile"
        values = {"CPP_COMPILE_CMD": cpp_compile_cmd}
        if language == "java":
            script_name = "java.compile"
            values = {"JAVA_COMPILE_CMD": java_compile_cmd}
        elif language == "py":
            script_name = "python.compile"
            values = {
                "PYTHON_COMPILE_FLAG_SUFFIX": python_compile_flag_suffix,
            }
        template = self._domjudge_load_script_asset(script_name)
        rendered = self._domjudge_render_script_template(template, values)
        return rendered.encode("utf-8")

    def _domjudge_cpp_executable_build_script(self, source_name: str, *, role: str) -> bytes:
        compiler = domjudge_text(getattr(self._constants, "TOOLCHAIN_CPP_COMPILER", "g++"), default="g++")
        safe_source = domjudge_path_name(source_name, default="interactor.cpp")
        safe_role = domjudge_text(role, default="executable")
        return (
            "#!/bin/sh\n"
            f"# Auto-generated build script for {safe_role} by Polygon2DOMjudge\n"
            f"{shlex.quote(compiler)} -Wall -DDOMJUDGE -O2 {shlex.quote(safe_source)} -std=gnu++20 -o run\n"
        ).encode("utf-8")

    def _domjudge_task_kind(
        self,
        payload: dict[str, object] | None = None,
        *,
        invocation_source: str | None = None,
        compile_only: object | None = None,
    ) -> str:
        payload_obj = payload if isinstance(payload, dict) else {}
        explicit = domjudge_lower_text(payload_obj.get("task_kind"))
        if explicit in self._TASK_KIND_SET:
            return explicit
        source = str(
            invocation_source
            if invocation_source is not None
            else payload_obj.get("invocation_source") or ""
        ).strip().lower()
        legacy_compile_only = self._domjudge_bool(
            compile_only if compile_only is not None else payload_obj.get("compile_only"),
            default=False,
        )
        if legacy_compile_only:
            return self._TASK_KIND_COMPILE_ONLY
        if source.startswith("build.generate") or source == "build.validate-tests":
            return self._TASK_KIND_GENERATE
        return self._TASK_KIND_SOLVE

    def _domjudge_execution_modes(
        self,
        payload: dict[str, object] | None = None,
        *,
        invocation_source: str | None = None,
        compile_only: object | None = None,
    ) -> tuple[bool, bool, bool]:
        payload_obj = payload if isinstance(payload, dict) else {}
        source = str(
            invocation_source
            if invocation_source is not None
            else payload_obj.get("invocation_source") or ""
        ).strip().lower()
        kind = self._domjudge_task_kind(
            payload_obj,
            invocation_source=invocation_source,
            compile_only=compile_only,
        )
        solve_main_mode = source in {"build.solve", "solve.main"}
        return (
            kind == self._TASK_KIND_COMPILE_ONLY,
            kind == self._TASK_KIND_GENERATE,
            solve_main_mode,
        )

    def _domjudge_run_script(
        self,
        interactive: bool,
        *,
        solve_mode: bool = False,
        compile_only: bool = False,
        generate_mode: bool = False,
    ) -> bytes:
        _ = bool(solve_mode)
        if interactive:
            return self._domjudge_load_script_asset("interactive.run").encode("utf-8")
        if bool(compile_only):
            script_name = "skip.run"
        elif bool(generate_mode):
            script_name = "generate.run"
        else:
            script_name = "normal.run"
        return self._domjudge_load_script_asset(script_name).encode("utf-8")

    def _domjudge_compare_script(
        self,
        *,
        solve_mode: bool = False,
        generate_mode: bool = False,
    ) -> bytes:
        if bool(solve_mode):
            script_name = "skip.compare"
        elif bool(generate_mode):
            script_name = "generate.compare"
        else:
            script_name = "normal.compare"
        return self._domjudge_load_script_asset(script_name).encode("utf-8")

    def domjudge_config(self) -> dict[str, object]:
        return domjudge_config_from_constants(self._constants)

    @staticmethod
    def domjudge_languages() -> list[dict[str, object]]:
        return domjudge_languages_payload()

    def domjudge_list_hosts(self) -> list[dict[str, object]]:
        with self._state_lock:
            return domjudge_hosts_payload(self._hosts_state)

    @staticmethod
    def _domjudge_script_ids(job_id: int) -> tuple[int, int, int]:
        return domjudge_script_ids(job_id)

    @staticmethod
    def _domjudge_parse_script_id(raw_id: object) -> tuple[int, int]:
        return domjudge_parse_script_id(raw_id)

    def _domjudge_script_provider_job_id(self, *, kind: str, script_hash: str, default_job_id: int) -> int:
        return domjudge_script_provider_job_id(
            kind=kind,
            script_hash=script_hash,
            default_job_id=default_job_id,
            fetch_rows=lambda field, safe_hash: self._db_fetch_all(
                f"""
                SELECT job_id,work_root
                FROM judgehost_domjudge_jobs
                WHERE {field}=?
                ORDER BY job_id ASC
                LIMIT 256
                """,
                [safe_hash],
            ),
        )

