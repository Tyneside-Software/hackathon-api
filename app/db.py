"""Datastore / Firestore access and in-memory last-known devices."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import BUS_TTL_S, log

# Last-known positions in this process. Datastore is the durable copy when GCP works.
last_devices: dict[str, dict] = {}
# Registered users in this process. Firestore/Datastore is the durable copy when GCP works.
last_users: dict[str, dict] = {}
# Shared live-bus snapshot. Firestore BusCache is the durable copy; this dict
# stops one Cloud Run instance hitting bustimes.org on every map poll.
last_buses: dict[str, dict] = {}


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


def _bus_row(data: dict, region: str) -> dict:
    vehicles = data.get("vehicles")
    if vehicles is None and data.get("vehicles_json"):
        try:
            vehicles = json.loads(data["vehicles_json"])
        except (TypeError, ValueError):
            vehicles = []
    if not isinstance(vehicles, list):
        vehicles = []
    return {
        "region": region,
        "fetched_at": as_iso(data.get("fetched_at")) or data.get("fetched_at"),
        "count": int(data.get("count") or len(vehicles)),
        "vehicles": vehicles,
    }


def _bus_age_s(row: dict | None) -> float | None:
    if not row:
        return None
    fetched = row.get("fetched_at")
    if not fetched:
        return None
    try:
        dt = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())


def load_bus_cache(region: str) -> dict | None:
    key = (region or "").strip() or "newcastle"
    mem = last_buses.get(key)
    age = _bus_age_s(mem) if mem else None
    if mem and mem.get("vehicles") is not None and age is not None and age < BUS_TTL_S:
        return dict(mem)
    try:
        from google.cloud import firestore

        snap = firestore.Client().collection("BusCache").document(key).get()
        if snap.exists:
            row = _bus_row(snap.to_dict() or {}, key)
            last_buses[key] = row
            return dict(row)
    except Exception as exc:
        log.warning("Firestore BusCache read failed: %s", exc)
    try:
        client = datastore_client()
        entity = client.get(client.key("BusCache", key))
        if entity is not None:
            row = _bus_row(dict(entity), key)
            last_buses[key] = row
            return dict(row)
    except Exception as exc:
        log.warning("Datastore BusCache read failed: %s", exc)
    if mem and mem.get("vehicles") is not None:
        return dict(mem)
    return None


def write_bus_cache(region: str, row: dict) -> str:
    key = (region or "").strip() or "newcastle"
    stored = {
        "region": key,
        "fetched_at": row.get("fetched_at") or utc_now(),
        "count": int(row.get("count") or len(row.get("vehicles") or [])),
        "vehicles": list(row.get("vehicles") or []),
    }
    last_buses[key] = stored
    wrote = "memory"
    try:
        from google.cloud import firestore

        firestore.Client().collection("BusCache").document(key).set(stored)
        wrote = "firestore"
    except Exception as exc:
        log.warning("Firestore BusCache write failed: %s", exc)
    try:
        client = datastore_client()
        entity = client.entity(
            key=client.key("BusCache", key),
            exclude_from_indexes=("vehicles_json",),
        )
        entity.update(
            {
                "region": key,
                "fetched_at": stored["fetched_at"],
                "count": stored["count"],
                "vehicles_json": json.dumps(stored["vehicles"], separators=(",", ":")),
            }
        )
        client.put(entity)
        if wrote == "memory":
            wrote = "datastore"
    except Exception as exc:
        log.warning("Datastore BusCache write failed: %s", exc)
    return wrote
