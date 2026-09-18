"""Last-known device positions."""
from __future__ import annotations

from fastapi import APIRouter

from ..db import get_device_row, list_device_rows

router = APIRouter()


@router.get("/v1/devices")
def list_devices() -> dict:
    """Last known position for every phone that has pinged."""
    rows = list_device_rows()
    return {"ok": True, "count": len(rows), "devices": rows}


@router.get("/v1/devices/{device_id}")
def get_device(device_id: str) -> dict:
    row = get_device_row(device_id)
    if not row:
        return {"ok": False, "message": "Device not found", "device_id": device_id}
    return {"ok": True, **row}
