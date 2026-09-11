"""Datastore / Firestore access and in-memory last-known devices."""
from __future__ import annotations

from datetime import datetime, timezone

from .config import log

# Last-known positions in this process. Datastore is the durable copy when GCP works.
last_devices: dict[str, dict] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def device_public(row: dict) -> dict:
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


def datastore_client():
    from google.cloud import datastore

    return datastore.Client()


def as_iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def ping_public(row: dict) -> dict:
    return {
        "device_id": row.get("device_id"),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "accuracy_m": row.get("accuracy_m"),
        "heading": row.get("heading"),
        "speed_mps": row.get("speed_mps"),
        "recorded_at": as_iso(row.get("recorded_at")),
        "received_at": as_iso(row.get("received_at")),
    }


def pings_from_firestore(device_id: str, take: int) -> list[dict]:
    from google.cloud import firestore

    db = firestore.Client()
    matched = []
    for snap in db.collection("LocationPing").stream():
        data = snap.to_dict() or {}
        if str(data.get("device_id") or "").strip() != device_id:
            continue
        matched.append(ping_public(data))
    matched.sort(key=lambda r: r.get("recorded_at") or "", reverse=True)
    return matched[:take]


def pings_from_datastore(device_id: str, take: int) -> list[dict]:
    client = datastore_client()
    matched = []
    for entity in client.query(kind="LocationPing").fetch(limit=2000):
        if str(entity.get("device_id") or "").strip() != device_id:
            continue
        matched.append(ping_public(dict(entity)))
    matched.sort(key=lambda r: r.get("recorded_at") or "", reverse=True)
    return matched[:take]
