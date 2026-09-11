"""Datastore / Firestore access and in-memory last-known devices."""
from __future__ import annotations

from datetime import datetime, timezone

from .config import log

# Last-known positions in this process. Datastore is the durable copy when GCP works.
last_devices: dict[str, dict] = {}
# Registered users in this process. Firestore/Datastore is the durable copy when GCP works.
last_users: dict[str, dict] = {}


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


def _user_row(data: dict, username: str) -> dict:
    return {
        "username": username,
        "email": data.get("email"),
        "full_name": data.get("full_name"),
        "disabled": bool(data.get("disabled", False)),
        "hashed_password": data.get("hashed_password") or "",
    }


def load_user_row(username: str) -> dict | None:
    key = username.strip()
    if not key:
        return None
    cached = last_users.get(key)
    if cached:
        return dict(cached)
    try:
        from google.cloud import firestore

        snap = firestore.Client().collection("User").document(key).get()
        if snap.exists:
            row = _user_row(snap.to_dict() or {}, key)
            last_users[key] = row
            return dict(row)
    except Exception as exc:
        log.warning("Firestore User read failed: %s", exc)
    try:
        client = datastore_client()
        entity = client.get(client.key("User", key))
        if entity is not None:
            row = _user_row(dict(entity), key)
            last_users[key] = row
            return dict(row)
    except Exception as exc:
        log.warning("Datastore User read failed: %s", exc)
    return None


def write_user_row(row: dict) -> None:
    username = str(row.get("username") or "").strip()
    stored = {
        "username": username,
        "email": row.get("email"),
        "full_name": row.get("full_name"),
        "disabled": bool(row.get("disabled", False)),
        "hashed_password": row.get("hashed_password") or "",
        "updated_at": utc_now(),
    }
    last_users[username] = stored
    try:
        from google.cloud import firestore

        firestore.Client().collection("User").document(username).set(stored)
    except Exception as exc:
        log.warning("Firestore User write failed: %s", exc)
    try:
        client = datastore_client()
        entity = client.entity(key=client.key("User", username))
        entity.update(stored)
        client.put(entity)
    except Exception as exc:
        log.warning("Datastore User write failed: %s", exc)
