"""Public fidget squish profile pictures, so other people can see them."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..database import PersistError, normalise_username
from ..models import ShopProfile

router = APIRouter()


class ProfileIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    photo: str | None = None
    full_name: str | None = None


@router.get("/katie/profiles")
def list_profiles():
    return {"users": ShopProfile.list_public()}


@router.post("/katie/profiles")
def save_profile(body: ProfileIn):
    name = normalise_username(body.username)
    if not name:
        raise HTTPException(status_code=400, detail="Need a username")
    photo = body.photo or ""
    if len(photo) > 500_000:
        raise HTTPException(status_code=400, detail="Picture is too large")
    try:
        ShopProfile.upsert(name, photo, body.full_name)
    except PersistError as exc:
        raise HTTPException(status_code=503, detail="Could not save picture") from exc
    return {"ok": True, "username": name}
