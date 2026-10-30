"""Account fleet feed. One bearer token, one account."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..dependencies import get_current_active_user
from ..empire import NEW_CAR_PENCE, buy_car, cancel_job, rename_company, sell_car, set_loiter, snapshot
from ..models import User

router = APIRouter()


class EmpireBuy(BaseModel):
    pence: int


class EmpireSell(BaseModel):
    sign: str


class EmpireCancel(BaseModel):
    sign: str


class EmpireLoiter(BaseModel):
    lat: float
    lng: float
    radius_km: float
    firm: str = ""


class EmpireCompany(BaseModel):
    name: str


@router.get("/v1/empire")
def empire_feed(current_user: Annotated[User, Depends(get_current_active_user)]) -> dict:
    return snapshot(current_user.username)


@router.post("/v1/empire/company")
def empire_company(
    body: EmpireCompany,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Rename this account's taxi company. The fleet stays."""
    if rename_company(current_user.username, body.name) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A company name needs 1 to 48 characters",
        )
    return snapshot(current_user.username)


@router.post("/v1/empire/buy")
def empire_buy(
    body: EmpireBuy,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Spend the purchase price to add T11 when the account can afford it. A smaller payment adds nothing."""
    sign = buy_car(current_user.username, body.pence)
    if sign is None:
        if int(body.pence) < NEW_CAR_PENCE:
            detail = f"{NEW_CAR_PENCE} pence buys the next car"
        else:
            detail = "Not enough money for that taxi"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    return {"sign": sign}


@router.post("/v1/empire/sell")
def empire_sell(
    body: EmpireSell,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Credit the sale price and remove an owned taxi. A passenger job and the last two stay."""
    if not sell_car(current_user.username, body.sign):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That taxi cannot be sold")
    return snapshot(current_user.username)


@router.post("/v1/empire/cancel")
def empire_cancel(
    body: EmpireCancel,
    current_user: Annotated[User, Depends(get_current_active_user)],
) -> dict:
    """Cancel a passenger job. Miles already driven become fuel, and there is no fare."""
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
