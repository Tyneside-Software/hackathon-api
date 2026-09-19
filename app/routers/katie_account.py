"""Katie shop accounts — email login plus Google / Apple / Facebook tokens."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..database import PersistError
from ..models import User

router = APIRouter()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
APPLE_CLIENT_ID = os.getenv("APPLE_CLIENT_ID", "")
FACEBOOK_APP_ID = os.getenv("FACEBOOK_APP_ID", "")


class GoogleIn(BaseModel):
    credential: str = Field(default="")


class AppleIn(BaseModel):
    identity_token: str = Field(default="")
    email: str | None = None
    name: str | None = None


class FacebookIn(BaseModel):
    access_token: str = Field(default="")


@router.get("/katie/auth/providers")
def katie_auth_providers() -> dict:
    return {
        "google_client_id": GOOGLE_CLIENT_ID or None,
        "apple_client_id": APPLE_CLIENT_ID or None,
        "facebook_app_id": FACEBOOK_APP_ID or None,
    }


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "fidget-squish"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


@router.post("/katie/auth/google")
def katie_auth_google(body: GoogleIn):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    token = (body.credential or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing Google credential")
    try:
        data = _http_json("https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(token))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Could not check Google sign-in") from exc
    if data.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Google sign-in did not match this shop")
    verified = data.get("email_verified")
    if verified not in (True, "true", "True"):
        raise HTTPException(status_code=401, detail="Google email is not verified")
    email = (data.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=401, detail="Google did not share an email")
    try:
        user = User.from_oauth(
            email=email,
            full_name=(data.get("name") or "").strip() or None,
            photo=(data.get("picture") or "").strip() or None,
        )
    except PersistError as exc:
        raise HTTPException(status_code=503, detail="Could not store user") from exc
    token_out = user.issue_token()
    body_out = user.public().model_dump()
    body_out["access_token"] = token_out.access_token
    body_out["token_type"] = "bearer"
    return body_out


@router.post("/katie/auth/apple")
def katie_auth_apple(body: AppleIn):
    if not APPLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Apple sign-in is not configured")
    token = (body.identity_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing Apple token")
    email = (body.email or "").strip()
    try:
        import jwt
        from jwt import PyJWKClient

        jwks = PyJWKClient("https://appleid.apple.com/auth/keys")
        signing_key = jwks.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_CLIENT_ID,
            issuer="https://appleid.apple.com",
        )
        email = email or (payload.get("email") or "")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Could not check Apple sign-in") from exc
    if not email:
        raise HTTPException(status_code=401, detail="Apple did not share an email")
    try:
        user = User.from_oauth(email=email, full_name=(body.name or "").strip() or None)
    except PersistError as exc:
        raise HTTPException(status_code=503, detail="Could not store user") from exc
    token_out = user.issue_token()
    body_out = user.public().model_dump()
    body_out["access_token"] = token_out.access_token
    body_out["token_type"] = "bearer"
    return body_out


@router.post("/katie/auth/facebook")
def katie_auth_facebook(body: FacebookIn):
    if not FACEBOOK_APP_ID:
        raise HTTPException(status_code=503, detail="Facebook sign-in is not configured")
    token = (body.access_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing Facebook token")
    try:
        data = _http_json(
            "https://graph.facebook.com/me?fields=id,name,email,picture.type(large)"
            "&access_token=" + urllib.parse.quote(token)
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Could not check Facebook sign-in") from exc
    email = (data.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=401, detail="Facebook did not share an email")
    picture = None
    pic = data.get("picture")
    if isinstance(pic, dict):
        picture = ((pic.get("data") or {}).get("url") or "").strip() or None
    try:
        user = User.from_oauth(
            email=email,
            full_name=(data.get("name") or "").strip() or None,
            photo=picture,
        )
    except PersistError as exc:
        raise HTTPException(status_code=503, detail="Could not store user") from exc
    token_out = user.issue_token()
    body_out = user.public().model_dump()
    body_out["access_token"] = token_out.access_token
    body_out["token_type"] = "bearer"
    return body_out
