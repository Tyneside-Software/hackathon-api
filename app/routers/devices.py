"""Last-known device positions and account links."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from ..database import PersistError, UserExistsError
from ..db import get_device_row, link_device, list_device_rows, list_linked_devices, set_device_nickname
from ..dependencies import get_current_active_user
from ..models import User
from ..schemas import DeviceLinkPayload

router = APIRouter()


@router.post("/v1/me/devices")
async def link_my_device(
    payload: DeviceLinkPayload,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Attach this phone's device_id to the signed-in account."""
    try:
        row = link_device(current_user.username, payload.device_id, payload.nickname)
    except UserExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PersistError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not link device",
        ) from exc
    return {"ok": True, **row}


@router.get("/v1/me/devices")
async def list_my_devices(
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    rows = list_linked_devices(current_user.username)
    return {"ok": True, "count": len(rows), "devices": rows}


@router.patch("/v1/me/devices")
async def nickname_my_device(
    payload: DeviceLinkPayload,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Set a nickname for a phone linked to this account."""
    nick = (payload.nickname or "").strip()
    if not nick:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nickname cannot be empty")
    try:
        row = set_device_nickname(current_user.username, payload.device_id, nick)
    except PersistError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, **row}


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
