#!/usr/bin/env python3
"""Hackathon API — FastAPI for Cloud Run."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

VERSION = "0.1.5"
log = logging.getLogger("hackathon-api")

app = FastAPI(title="Hackathon API", version=VERSION)


class FieldPayload(BaseModel):
    key: str
    value: str


class LocationPayload(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float | None = None
    recorded_at: str | None = None
    heading: float | None = None
    speed_mps: float | None = None


# Last-known positions in this process. Datastore is the durable copy when GCP works.
_last_devices: dict[str, dict] = {}

_raw = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,"
    "https://hackathon.tyneside.software,https://michaelthomsoncc.github.io",
)
origins = [o.strip() for o in _raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
    max_age=600,
)


@app.get("/")
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


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "hackathon-api",
        "utc": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
    }


@app.post("/create_field")
def create_field(payload: FieldPayload) -> dict:
    """Create or update a field in Datastore by key."""
    from google.cloud import datastore

    client = datastore.Client()
    entity_key = client.key("Field", payload.key)
    entity = datastore.Entity(key=entity_key)
    entity.update(
        {
            "value": payload.value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    client.put(entity)

    return {"ok": True, "message": "Field stored", "key": payload.key, "value": payload.value}


@app.get("/view_field/{key}")
def view_field(key: str) -> dict:
    """Retrieve a field value from Datastore by key."""
    from google.cloud import datastore

    client = datastore.Client()
    entity_key = client.key("Field", key)
    entity = client.get(entity_key)
    if entity is None:
        return {"ok": False, "message": "Field not found", "key": key}

    return {"ok": True, "key": key, "value": entity.get("value")}


@app.get("/test_field")
def test_field() -> dict:
    return {"ok": True, "key": "example_key", "value": "example_value"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _device_public(row: dict) -> dict:
    return {
        "device_id": row.get("device_id"),
        "last_lat": row.get("last_lat"),
        "last_lng": row.get("last_lng"),
        "last_seen_at": row.get("last_seen_at"),
        "accuracy_m": row.get("accuracy_m"),
        "heading": row.get("heading"),
        "speed_mps": row.get("speed_mps"),
        "updated_at": row.get("updated_at"),
    }


def _datastore_client():
    from google.cloud import datastore

    return datastore.Client()


def _as_iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _ping_public(row: dict) -> dict:
    return {
        "device_id": row.get("device_id"),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "accuracy_m": row.get("accuracy_m"),
        "heading": row.get("heading"),
        "speed_mps": row.get("speed_mps"),
        "recorded_at": _as_iso(row.get("recorded_at")),
        "received_at": _as_iso(row.get("received_at")),
    }


def _pings_from_firestore(device_id: str, take: int) -> list[dict]:
    from google.cloud import firestore

    db = firestore.Client()
    matched = []
    for snap in db.collection("LocationPing").stream():
        data = snap.to_dict() or {}
        if str(data.get("device_id") or "").strip() != device_id:
            continue
        matched.append(_ping_public(data))
    matched.sort(key=lambda r: r.get("recorded_at") or "", reverse=True)
    return matched[:take]


def _pings_from_datastore(device_id: str, take: int) -> list[dict]:
    client = _datastore_client()
    matched = []
    for entity in client.query(kind="LocationPing").fetch(limit=2000):
        if str(entity.get("device_id") or "").strip() != device_id:
            continue
        matched.append(_ping_public(dict(entity)))
    matched.sort(key=lambda r: r.get("recorded_at") or "", reverse=True)
    return matched[:take]


@app.post("/v1/locations")
def create_location(payload: LocationPayload) -> dict:
    """Phone GPS ping. Upserts last-known Device; appends LocationPing history."""
    now = _utc_now()
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
    _last_devices[device_id] = row
    stored = "memory"
    try:
        from google.cloud import datastore

        client = _datastore_client()
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
    return {"ok": True, "stored": stored, **_device_public(row)}


@app.get("/v1/devices")
def list_devices() -> dict:
    """Last known position for every phone that has pinged."""
    devices = {k: dict(v) for k, v in _last_devices.items()}
    try:
        client = _datastore_client()
        for entity in client.query(kind="Device").fetch(limit=200):
            device_id = entity.key.name or entity.get("device_id")
            if not device_id:
                continue
            devices[device_id] = _device_public({**dict(entity), "device_id": device_id})
    except Exception as exc:
        log.warning("Datastore list failed, using in-memory devices: %s", exc)
    rows = [_device_public(d) for d in devices.values()]
    rows.sort(key=lambda d: d.get("last_seen_at") or "", reverse=True)
    return {"ok": True, "count": len(rows), "devices": rows}


@app.get("/v1/devices/{device_id}")
def get_device(device_id: str) -> dict:
    cached = _last_devices.get(device_id)
    if cached:
        return {"ok": True, **_device_public(cached)}
    try:
        client = _datastore_client()
        entity = client.get(client.key("Device", device_id))
    except Exception as exc:
        log.warning("Datastore read failed: %s", exc)
        return {"ok": False, "message": "Device not found", "device_id": device_id}
    if entity is None:
        return {"ok": False, "message": "Device not found", "device_id": device_id}
    return {"ok": True, **_device_public({**dict(entity), "device_id": device_id})}


@app.get("/v1/locations")
def list_locations(device_id: str, limit: int = 100) -> dict:
    """Historic GPS pings for one phone, from Firestore LocationPing (Datastore fallback)."""
    take = max(1, min(limit, 500))
    wanted = device_id.strip()
    rows: list[dict] = []
    source = "none"
    try:
        rows = _pings_from_firestore(wanted, take)
        if rows:
            source = "firestore"
    except Exception as exc:
        log.warning("Firestore LocationPing read failed: %s", exc)
    if not rows:
        try:
            rows = _pings_from_datastore(wanted, take)
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


if __name__ == "__main__":
    import uvicorn

    # Pass the app object (not "app.main:app") so this works when the
    # Cloud Build trigger execs GOOGLE_ENTRYPOINT=app/main.py.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
