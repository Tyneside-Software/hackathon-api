"""Root and health routes. Keep these free of Datastore."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from ..config import VERSION

router = APIRouter()


@router.get("/")
def root() -> dict:
    return {
        "service": "hackathon-api",
        "docs": "/docs",
        "health": "/health",
        "test_field": "/test_field",
        "locations": "/v1/locations",
        "devices": "/v1/devices",
        "version": VERSION,
    }


@router.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "hackathon-api",
        "utc": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
    }
