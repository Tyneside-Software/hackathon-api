"""Katie shop friends — search accounts and add people."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..database import PersistError, normalise_username
from ..dependencies import get_current_active_user
from ..models import FriendLink, ShopAdmin, User

router = APIRouter()


class FriendIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)


@router.get("/katie/users/search")
def search_users(
    q: str = Query(default=""),
    current_user: User = Depends(get_current_active_user),
):
    needle = (q or "").strip()
    friends = FriendLink.usernames_for(current_user.username)
    admins = ShopAdmin.usernames()
    users = User.search(needle, exclude=current_user.username, limit=24 if not needle else 8)
    return {
        "users": [u.card(friend=u.username in friends, admin=u.username in admins) for u in users]
    }


@router.get("/katie/friends")
def list_friends(current_user: User = Depends(get_current_active_user)):
    people = FriendLink.list_for(current_user.username)
    admins = ShopAdmin.usernames()
    return {"users": [u.card(friend=True, admin=u.username in admins) for u in people]}


@router.post("/katie/friends")
def add_friend(body: FriendIn, current_user: User = Depends(get_current_active_user)):
    name = normalise_username(body.username)
    if name == current_user.username:
        raise HTTPException(status_code=400, detail="That’s you.")
    other = User.get(name)
    if other is None or other.disabled:
        raise HTTPException(status_code=404, detail="No account with that name.")
    try:
        FriendLink.add(current_user.username, name)
    except PersistError as exc:
        raise HTTPException(status_code=503, detail="Could not add friend") from exc
    return other.card(friend=True, admin=ShopAdmin.has(other.username))


@router.delete("/katie/friends/{username}")
def remove_friend(username: str, current_user: User = Depends(get_current_active_user)):
    FriendLink.remove(current_user.username, username)
    return {"ok": True}
