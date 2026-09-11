"""GPS pings and ping history."""
from __future__ import annotations

from fastapi import APIRouter

from ..config import log
from ..db import (
    datastore_client,
    device_public,
    last_devices,
    pings_from_datastore,
    pings_from_firestore,
    utc_now,
)
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
    last_devices[device_id] = row
    stored = "memory"
    try:
        from google.cloud import datastore

        client = datastore_client()
        ping = datastore.Entity(key=client.key("LocationPing"))
        ping.update(
            {
                "device_id": device_id,
                "lat": payload.lat,
                "lng": payload.lng,
                "accuracy_m": payload.accuracy_m,
                "recorded_at": recorded,
                "received_at": now,
                "heading": payload.heading,
                "speed_mps": payload.speed_mps,
            }
        )
        client.put(ping)
        device = datastore.Entity(key=client.key("Device", device_id))
        device.update(row)
        client.put(device)
        stored = "datastore"
    except Exception as exc:
        log.warning("Datastore write failed, keeping in-memory last known: %s", exc)
    return {"ok": True, "stored": stored, **device_public(row)}


@router.get("/v1/locations")
def list_locations(device_id: str, limit: int = 100) -> dict:
    """Historic GPS pings for one phone, from Firestore LocationPing (Datastore fallback)."""
    take = max(1, min(limit, 500))
    wanted = device_id.strip()
    rows: list[dict] = []
    source = "none"
    try:
        rows = pings_from_firestore(wanted, take)
        if rows:
            source = "firestore"
    except Exception as exc:
        log.warning("Firestore LocationPing read failed: %s", exc)
    if not rows:
        try:
            rows = pings_from_datastore(wanted, take)
            if rows:
                source = "datastore"
        except Exception as exc:
            log.warning("Datastore LocationPing read failed: %s", exc)
    return {
        "ok": True,
        "device_id": wanted,
        "count": len(rows),
        "source": source,
        "pings": rows,
    }
