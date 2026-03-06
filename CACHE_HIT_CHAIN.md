# CACHE_HIT_CHAIN

Status: archived note.

This file used to describe a detailed step-by-step judgehost cache hit call chain.
The implementation has changed multiple times (cache/index schema, lock scope, startup policy),
so line-level flow notes in older revisions are no longer reliable.

Use these sources of truth instead:

- `HASH_SCHEMA.md` for hash/signature contracts
- `AGENTS.md` for current architecture constraints
- `app/services/judgehost_service.py`
- `app/services/judge_fs_index_service.py`

If detailed chain analysis is needed again, regenerate from current code and replace this file.
