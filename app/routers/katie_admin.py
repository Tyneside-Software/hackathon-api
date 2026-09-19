"""Katie shop Admin login — password is checked here, not in the browser."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..database import normalise_username
from ..models import User
from ..security import decode_access_token

router = APIRouter()

_KATIE_PASSWORD = os.getenv("KATIE_ADMIN_PASSWORD", "cassiethecat")
_KATIE_TOKEN_SECRET = os.getenv("KATIE_ADMIN_TOKEN_SECRET") or secrets.token_hex(32)
_KATIE_ADMIN_USER = normalise_username(os.getenv("KATIE_ADMIN_USERNAME", "LewisThomson"))
_ONLY_MSG = "Only LewisThomson can open Admin. Log in on Account first."


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
        if user != _KATIE_ADMIN_USER:
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


class KatieLoginIn(BaseModel):
    password: str = Field(default="")


class KatieTokenIn(BaseModel):
    token: str = Field(default="")


@router.post("/katie/admin/login")
def katie_admin_login(body: KatieLoginIn, request: Request):
    if not _password_ok((body.password or "").strip()):
        return JSONResponse({"ok": False, "detail": "Wrong password."}, status_code=401)
    user = _account_from_request(request)
    if not user or normalise_username(user.username) != _KATIE_ADMIN_USER:
        return JSONResponse({"ok": False, "detail": _ONLY_MSG}, status_code=403)
    return {"ok": True, "token": _issue_token(user.username)}


@router.post("/katie/admin/verify")
def katie_admin_verify(body: KatieTokenIn, request: Request):
    token_user = _token_user((body.token or "").strip())
    if not token_user:
        return JSONResponse({"ok": False}, status_code=401)
    user = _account_from_request(request)
    if not user or normalise_username(user.username) != token_user:
        return JSONResponse({"ok": False, "detail": _ONLY_MSG}, status_code=403)
    return {"ok": True}
