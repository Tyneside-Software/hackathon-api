"""Account fleet feed. One bearer token, one account."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ..dependencies import get_current_active_user
from ..empire import snapshot
from ..models import User

router = APIRouter()


@router.get("/v1/empire")
def empire_feed(current_user: Annotated[User, Depends(get_current_active_user)]) -> dict:
    return snapshot(current_user.username)
