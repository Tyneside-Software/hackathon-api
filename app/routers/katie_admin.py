"""Katie shop Admin login — password is checked here, not in the browser."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()

_KATIE_PASSWORD = os.getenv("KATIE_ADMIN_PASSWORD", "cassiethecat")
_KATIE_TOKEN_SECRET = os.getenv("KATIE_ADMIN_TOKEN_SECRET") or secrets.token_hex(32)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(body: str) -> str:
    return _b64(
        hmac.new(_KATIE_TOKEN_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    )


def _issue_token() -> str:
    payload = json.dumps(
        {"exp": int(time.time()) + 12 * 3600, "n": secrets.token_hex(16)},
        separators=(",", ":"),
    )
    body = _b64(payload.encode("utf-8"))
    return body + "." + _sign(body)


def _token_ok(token: str) -> bool:
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(body)):
            return False
        pad = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + pad))
        return int(data.get("exp", 0)) > time.time()
    except Exception:
        return False


def _password_ok(typed: str) -> bool:
    got = hashlib.sha256(typed.encode("utf-8")).digest()
    want = hashlib.sha256(_KATIE_PASSWORD.encode("utf-8")).digest()
    return hmac.compare_digest(got, want)


class KatieLoginIn(BaseModel):
    password: str = Field(default="")


class KatieTokenIn(BaseModel):
    token: str = Field(default="")


@router.post("/katie/admin/login")
def katie_admin_login(body: KatieLoginIn):
    if not _password_ok((body.password or "").strip()):
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True, "token": _issue_token()}


@router.post("/katie/admin/verify")
def katie_admin_verify(body: KatieTokenIn):
    if not _token_ok((body.token or "").strip()):
        return JSONResponse({"ok": False}, status_code=401)
    return {"ok": True}
