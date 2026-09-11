"""Last-known device positions."""
from __future__ import annotations

from fastapi import APIRouter

from ..config import log
from ..db import datastore_client, device_public, last_devices

router = APIRouter()


@router.get("/v1/devices")
def list_devices() -> dict:
    """Last known position for every phone that has pinged."""
    devices = {k: dict(v) for k, v in last_devices.items()}
    try:
        client = datastore_client()
        for entity in client.query(kind="Device").fetch(limit=200):
            device_id = entity.key.name or entity.get("device_id")
            if not device_id:
                continue
            devices[device_id] = device_public({**dict(entity), "device_id": device_id})
    except Exception as exc:
        log.warning("Datastore list failed, using in-memory devices: %s", exc)
    rows = [device_public(d) for d in devices.values()]
    rows.sort(key=lambda d: d.get("last_seen_at") or "", reverse=True)
    return {"ok": True, "count": len(rows), "devices": rows}


@router.get("/v1/devices/{device_id}")
def get_device(device_id: str) -> dict:
    cached = last_devices.get(device_id)
    if cached:
        return {"ok": True, **device_public(cached)}
    try:
        client = datastore_client()
        entity = client.get(client.key("Device", device_id))
    except Exception as exc:
        log.warning("Datastore read failed: %s", exc)
        return {"ok": False, "message": "Device not found", "device_id": device_id}
    if entity is None:
        return {"ok": False, "message": "Device not found", "device_id": device_id}
    return {"ok": True, **device_public({**dict(entity), "device_id": device_id})}
