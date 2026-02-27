#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import DB
from app.services.runtime_cache_service import RuntimeCacheService
from app.settings import load_settings


def main() -> None:
    settings = load_settings()
    db = DB(settings.db_path)
    db.init()
    cleaner = RuntimeCacheService(db, settings.artifacts_root, settings.run_root)
    ran = cleaner.cleanup_cache(force=True)
    print("runtime_cache_cleanup_ok" if ran else "runtime_cache_cleanup_skipped")


if __name__ == "__main__":
    main()
