"""GPS pings and ping history."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..db import PersistError, device_public, list_pings, save_location, utc_now
from ..schemas import LocationPayload

router = APIRouter()


@router.post("/v1/locations")
def create_location(payload: LocationPayload) -> dict:
    """Phone GPS ping. Upserts last-known Device; appends LocationPing history."""
    now = utc_now()
    recorded = payload.recorded_at or now
    device_id = payload.device_id.strip()
    row = {
        "device_id": device_id,
        "last_lat": payload.lat,
        "last_lng": payload.lng,
        "last_seen_at": recorded,
        "accuracy_m": payload.accuracy_m,
        "heading": payload.heading,
        "speed_mps": payload.speed_mps,
        "updated_at": now,
    }
    ping = {
        "lat": payload.lat,
        "lng": payload.lng,
        "accuracy_m": payload.accuracy_m,
        "heading": payload.heading,
        "speed_mps": payload.speed_mps,
        "recorded_at": recorded,
        "received_at": now,
    }
    try:
        stored = save_location(row, ping)
    except PersistError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not store location",
        ) from exc
    return {"ok": True, "stored": stored, **device_public(row)}


@router.get("/v1/locations")
def list_locations(device_id: str, limit: int = 100) -> dict:
    """Historic GPS pings for one phone, from SQLite."""
    take = max(1, min(limit, 500))
    wanted = device_id.strip()
    rows = list_pings(wanted, take)
    return {
        "ok": True,
        "device_id": wanted,
        "count": len(rows),
        "source": "sqlite",
        "pings": rows,
    }
