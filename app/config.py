"""App-wide settings."""
from __future__ import annotations

import logging
import os
import secrets

VERSION = "0.1.6"
log = logging.getLogger("hackathon-api")

_raw = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,"
    "https://hackathon.tyneside.software,https://michaelthomsoncc.github.io",
)
origins = [o.strip() for o in _raw.split(",") if o.strip()]

# openssl rand -hex 32 — set JWT_SECRET_KEY on Cloud Run so tokens survive restarts.
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY") or os.getenv("SECRET_KEY") or ""
if not JWT_SECRET_KEY:
    JWT_SECRET_KEY = secrets.token_hex(32)
    log.warning("JWT_SECRET_KEY unset; tokens are only valid in this process")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))
