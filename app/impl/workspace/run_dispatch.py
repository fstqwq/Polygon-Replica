from __future__ import annotations

import secrets

from app.impl.runtime.config import config


def allocate_run_id() -> str:
    for _ in range(8):
        candidate = f'r-{secrets.token_hex(6)}'
        if config.db.fetch_one('SELECT id FROM runs WHERE id=?', [candidate]) is None:
            return candidate
    return f'r-{secrets.token_hex(8)}'


