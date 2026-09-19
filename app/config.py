"""App-wide settings."""
from __future__ import annotations

import logging
import os
import secrets

VERSION = "0.1.18"
log = logging.getLogger("hackathon-api")

# Live buses: one upstream fetch shared by every map tab. TTL is how long we
# serve SQLite before asking bustimes.org again. No looking clients
# means no GET /v1/buses, which means no upstream hit.
BUS_REGION = "newcastle"
BUS_TTL_S = 15
BUS_TRAIL_S = 10 * 60
BUS_MILES = 30
BUS_CENTRE = (54.9783, -1.6178)
BUS_BBOX = {
    "ymin": 54.5446,
    "ymax": 55.4120,
    "xmin": -2.3740,
    "xmax": -0.8616,
}
BUS_UPSTREAM = (
    "https://bustimes.org/vehicles.json"
    f"?ymin={BUS_BBOX['ymin']}&ymax={BUS_BBOX['ymax']}"
    f"&xmin={BUS_BBOX['xmin']}&xmax={BUS_BBOX['xmax']}"
)

_raw = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,"
    "https://hackathon.tyneside.software,https://tyneside.software,"
    "https://michaelthomsoncc.github.io",
)
origins = [o.strip() for o in _raw.split(",") if o.strip()]
for extra in (
    "https://hackathon.tyneside.software",
    "https://tyneside.software",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
):
    if extra not in origins:
        origins.append(extra)

# openssl rand -hex 32 — set JWT_SECRET_KEY on Cloud Run so tokens survive restarts.
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY") or os.getenv("SECRET_KEY") or ""
if not JWT_SECRET_KEY:
    JWT_SECRET_KEY = secrets.token_hex(32)
    log.warning("JWT_SECRET_KEY unset; tokens are only valid in this process")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120"))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_default_db = os.path.join(_ROOT, "hackathon.db").replace("\\", "/")
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{_default_db}"
