"""Katie shop Admin login — password is checked here, not in the browser."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..database import PersistError, normalise_username
from ..models import ShopAdmin, User
from ..security import decode_access_token

router = APIRouter()

_KATIE_PASSWORD = os.getenv("KATIE_ADMIN_PASSWORD", "cassiethecat")
_KATIE_TOKEN_SECRET = os.getenv("KATIE_ADMIN_TOKEN_SECRET") or secrets.token_hex(32)
_ONLY_MSG = "Only Admins can open Admin. Log in on Account first."


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(body: str) -> str:
    return _b64(
        hmac.new(_KATIE_TOKEN_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    )


def _issue_token(username: str) -> str:
    payload = json.dumps(
        {
            "exp": int(time.time()) + 12 * 3600,
            "n": secrets.token_hex(16),
            "u": normalise_username(username),
        },
        separators=(",", ":"),
    )
    body = _b64(payload.encode("utf-8"))
    return body + "." + _sign(body)


def _token_user(token: str) -> str | None:
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        pad = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + pad))
        if int(data.get("exp", 0)) <= time.time():
            return None
        user = normalise_username(str(data.get("u") or ""))
        if not user or not ShopAdmin.has(user):
            return None
        return user
    except Exception:
        return None


def _password_ok(typed: str) -> bool:
    got = hashlib.sha256(typed.encode("utf-8")).digest()
    want = hashlib.sha256(_KATIE_PASSWORD.encode("utf-8")).digest()
    return hmac.compare_digest(got, want)


def _account_from_request(request: Request) -> User | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    username = decode_access_token(header.split(" ", 1)[1].strip())
    if not username:
        return None
    return User.get(username)


def _admin_session(request: Request, token: str = "") -> tuple[User | None, JSONResponse | None]:
    header_tok = (request.headers.get("x-katie-admin") or "").strip()
    admin_tok = (token or header_tok).strip()
    token_user = _token_user(admin_tok)
    if not token_user:
        return None, JSONResponse({"ok": False}, status_code=401)
    user = _account_from_request(request)
    if not user or normalise_username(user.username) != token_user or not ShopAdmin.has(user.username):
        return None, JSONResponse({"ok": False, "detail": _ONLY_MSG}, status_code=403)
    return user, None


class KatieLoginIn(BaseModel):
    password: str = Field(default="")


class KatieTokenIn(BaseModel):
    token: str = Field(default="")


class AdminPersonIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)


@router.post("/katie/admin/login")
def katie_admin_login(body: KatieLoginIn, request: Request):
    if not _password_ok((body.password or "").strip()):
        return JSONResponse({"ok": False, "detail": "Wrong password."}, status_code=401)
    user = _account_from_request(request)
    if not user or not ShopAdmin.has(user.username):
        return JSONResponse({"ok": False, "detail": _ONLY_MSG}, status_code=403)
    return {"ok": True, "token": _issue_token(user.username)}


@router.post("/katie/admin/verify")
def katie_admin_verify(body: KatieTokenIn, request: Request):
    user, err = _admin_session(request, body.token)
    if err:
        return err
    return {"ok": True}


@router.get("/katie/admin/people")
def list_admins(request: Request):
    _user, err = _admin_session(request)
    if err:
        return err
    return {"users": ShopAdmin.people()}


@router.get("/katie/admin/people/search")
def search_admins(request: Request, q: str = Query(default="")):
    user, err = _admin_session(request)
    if err:
        return err
    needle = (q or "").strip()
    admins = ShopAdmin.usernames()
    found = User.search(needle, exclude=user.username, limit=24 if not needle else 8)
    return {"users": [u.card(admin=u.username in admins) for u in found]}


@router.post("/katie/admin/people")
def add_admin(body: AdminPersonIn, request: Request):
    user, err = _admin_session(request)
    if err:
        return err
    name = normalise_username(body.username)
    if name == user.username:
        return JSONResponse({"ok": False, "detail": "That’s you — you’re already an Admin."}, status_code=400)
    other = User.get(name)
    if other is None or other.disabled:
        return JSONResponse({"ok": False, "detail": "No account with that name."}, status_code=404)
    try:
        added = ShopAdmin.add(name)
    except PersistError:
        return JSONResponse({"ok": False, "detail": "Could not add Admin."}, status_code=503)
    card = added.card(admin=True)
    card["founder"] = False
    return card


@router.delete("/katie/admin/people/{username}")
def remove_admin(username: str, request: Request):
    _user, err = _admin_session(request)
    if err:
        return err
    try:
        ShopAdmin.remove(username)
    except PersistError as exc:
        if str(exc) == "founder":
            return JSONResponse(
                {"ok": False, "detail": "LewisThomson has to stay an Admin."},
                status_code=400,
            )
        if str(exc) == "last":
            return JSONResponse(
                {"ok": False, "detail": "There has to be at least one Admin."},
                status_code=400,
            )
        return JSONResponse({"ok": False, "detail": "Could not remove Admin."}, status_code=503)
    return {"ok": True}
