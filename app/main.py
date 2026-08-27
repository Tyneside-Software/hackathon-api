"""Hackathon API — FastAPI for Cloud Run."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Hackathon API", version="0.1.0")

_raw = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,https://michaelthomsoncc.github.io",
)
origins = [o.strip() for o in _raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
    max_age=600,
)


@app.get("/")
def root() -> dict:
    return {"service": "hackathon-api", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "hackathon-api",
        "utc": datetime.now(timezone.utc).isoformat(),
    }
