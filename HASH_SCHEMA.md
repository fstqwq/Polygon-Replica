# HASH_SCHEMA

This document records active cross-module hash contracts.

Conventions:

- Canonical JSON means sorted keys + compact separators `(',', ':')`.
- SHA-256 outputs are lowercase 64-hex strings.
- MD5 outputs are lowercase 32-hex strings.

Primary helpers live in `app/service/hashing.py`.

## 1) Build Ref

Producer: `FsManager.compute_build_ref(payload)`

- Input: canonical JSON payload
- Output: `sha256(payload)`
- Use: artifact object path key (`<hh>/<build_ref>`)

## 2) Build Cache Key

Producer: `BuildService._build_cache_key(...)`

Fields:

- `problem_id`
- `workspace_id`
- `source_commit`
- `source_ref`
- `generation_params_digest`
- `toolchain_cmd_digest`
- `sample_only`
- `schema` (`BUILD_CACHE_SCHEMA`, current `v3`)

Digest:

- `cache_key_hash = sha256(canonical_json(key))`

Derived build ref:

- `compute_build_ref({"schema": BUILD_CACHE_SCHEMA, "cache_key_hash": ...})`

## 3) Generation Params Digest

Producer: `BuildService._generation_params_digest(...)`

Fields:

- `schema` (`v1`)
- `sample_only`
- `build_config`
- `runtime_config`
- `tests_spec_rows`

Digest:

- `sha256(canonical_json(payload))`

## 4) Toolchain Command Digest

Producer: `compile_command_digest(command, flags)`

Fields:

- normalized `command`
- normalized `flags`

Digest:

- `sha256(canonical_json({command, flags}))`

## 5) Preview Ref

Producer: `PreviewService._preview_ref(...)`

Fields:

- `schema` (`preview-ref.v1`)
- `problem_id`
- `workspace_id`
- `source_commit` (or `__dirty__`)
- `source_ref`
- `statement_signature`
- `dynamic_samples`

Digest:

- `sha256(canonical_json(payload))`

## 6) Statement Signature (Quick FP)

Producer: `statement_sources_signature(...)`

Contract:

- Non-streaming quick fingerprint only.
- Input is structured `entries` list with metadata states, including `size` and `mtime_ns`.
- Output: `quick_fp_digest(entries, schema='statement-signature.v2')`.

## 7) Verification Signature (Quick FP)

Producer: `_verification_sources_signature_from_targets(...)`

Contract:

- Non-streaming quick fingerprint only.
- Input is structured `entries` list for file targets and dir targets (`state`, `size`, `mtime_ns`).
- Output: `quick_fp_digest(entries, schema='verification-signature.v2')`.

## 8) Async Task Cache

Producer: `AsyncTaskCacheService`

Key hash:

- `sha256(canonical_json(key_parts))`

Integrity marker:

- `meta_hash = sha256(canonical_json(meta))`
- `integrity_hash = sha256(meta_hash_bytes)`
- marker filename is `integrity_hash`

Note:

- Current metadata map is in-memory (`_entries`) with filesystem integrity markers.

## 9) Judge FS Index

Producer: `JudgeFsIndexService`

Entry key:

- `kind` (`case` / `solve-output`)
- `key_hash` (64-hex)
- `signature` (64-hex)

`signature(payload)`:

- dict/list/tuple => canonical JSON string
- other types => `str(payload)`
- final => SHA-256

File-set integrity:

- each file: `sha256(blob)` + `size`
- aggregate hash: `sha256(concat(file_sha256_bytes in sorted filename order))`
- marker filename stores aggregate hash

## 10) Judgehost Cache Refs

Producer: `JudgehostTaskService`

Case cache key/signature:

- key hash:
  - `testcase_hash`
  - for `build.solve` / `solve.main`: `testcase_hash = testcase_input_hash`
  - for other tasks: `testcase_hash = sha256_hash_of_hashes([sha256(input), sha256(answer)])`
- signature payload:
  - `schema = "case-cache"`
  - `source_hash`
  - `compile_hash`
  - `run_hash`
  - `compare_hash`
  - `compile_config_hash`
  - `run_config_hash`
  - `compare_config_hash`
  - `toolchain_cmd_digest`

Solve-output cache key/signature:

- key hash:
  - `testcase_input_hash`
- signature payload:
  - `schema = "solve-output-cache"`
  - `source_hash`
  - `compile_hash`
  - `run_hash`
  - `compile_config_hash`
  - `run_config_hash`
  - `toolchain_cmd_digest`

Solve/verify answer consistency contract:

- solve-main stores output cache by `(source(main), testcase_input_hash, compile/run config)`
- solve-verify cache/shortcut requires answer consistency via `testcase_answer_hash` vs solve-main `output_hash`
- per-test invalidation is isolated because key hash is testcase-scoped

Both use `JudgeFsIndexService.signature(payload)`.

## 11) Judgehost Manifest Digest

Producer: `_domjudge_manifest_digest(...)`

Canonical row fields:

- `path`
- `blob_key`
- `sha256`
- `size`
- `mode`

Digest:

- sort rows by `(path, blob_key, sha256, size, mode)`
- `sha256(canonical_json(rows))`

Validation contract:

- manifest digest match
- manifest path set equals file map path set
- per-file `sha256/size` match
- blob bytes hash/size match

## 12) DOMjudge Executable Hash Compatibility

Producer: `domjudge_executable_hash(files)`

Digest:

- For each file (sorted by filename): `md5(content) + filename + exec_flag_token`
- Concatenate tokens
- MD5 of concatenation

This matches DOMjudge judgedaemon executable hash behavior.
