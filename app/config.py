"""App-wide settings."""
from __future__ import annotations

import logging
import os

VERSION = "0.1.5"
log = logging.getLogger("hackathon-api")

_raw = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,"
    "https://hackathon.tyneside.software,https://michaelthomsoncc.github.io",
)
origins = [o.strip() for o in _raw.split(",") if o.strip()]
