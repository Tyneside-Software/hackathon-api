"""Account fleet feed. One bearer token, one account."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..dependencies import get_current_active_user
from ..empire import buy_car, snapshot
from ..models import User

router = APIRouter()


class EmpireBuy(BaseModel):
    pence: int


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
