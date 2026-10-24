"""Account fleet feed. One bearer token, one account."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..dependencies import get_current_active_user
from ..empire import buy_car, cancel_job, set_loiter, snapshot
from ..models import User

router = APIRouter()


class EmpireBuy(BaseModel):
    pence: int


class EmpireCancel(BaseModel):
    sign: str


class EmpireLoiter(BaseModel):
    lat: float
    lng: float
    radius_km: float
    firm: str = ""


@router.get("/v1/empire")
def empire_feed(current_user: Annotated[User, Depends(get_current_active_user)]) -> dict:
    return snapshot(current_user.username)


@router.post("/v1/empire/buy")
def empire_buy(
    body: EmpireBuy,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Spend 800000 pence to add T11. A smaller payment adds nothing."""
    sign = buy_car(current_user.username, body.pence)
    if sign is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="800000 pence buys the next car")
    return {"sign": sign}


@router.post("/v1/empire/cancel")
def empire_cancel(
    body: EmpireCancel,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Cancel a passenger job before the car moves. No fare and no fuel."""
    if not cancel_job(current_user.username, body.sign):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That job is not waiting to move")
    return snapshot(current_user.username)


@router.post("/v1/empire/loiter")
def empire_loiter(
    body: EmpireLoiter,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Set the patch where that firm's idle taxis loiter. A bad patch changes nothing."""
    if not set_loiter(current_user.username, body.lat, body.lng, body.radius_km, body.firm):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A loiter patch needs a point inside the circle, a radius, and the firm",
        )
    return snapshot(current_user.username)
