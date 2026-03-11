from __future__ import annotations

import base64  
import json  
import platform  
import re  
import secrets  
import shutil  
import sqlite3  
import time  
import warnings  
from datetime import datetime, timedelta, timezone  
from pathlib import Path  
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse  

from fastapi import HTTPException, Request  
from fastapi.responses import RedirectResponse  

from app.db import now_iso  
from app.impl.runtime.config import config
from app.service.platform.hashing import hmac_sha256_hex, sha256_hex_bytes, sha256_hex_text  

_C = config.constants
_RUNTIME_PROFILE_CACHE: dict[str, str] | None = None
_RUNTIME_PROFILE_MAX_LEN = 160
_RUNTIME_BACKEND_CACHE: dict[str, str] | None = None
_RUNTIME_BACKEND_CACHE_TS = 0.0
_RUNTIME_BACKEND_CACHE_TTL_SEC = 2.0
