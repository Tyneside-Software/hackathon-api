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
    access_token: str = Field(default="")


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


def _google_ok_audience(data: dict) -> bool:
    aud = data.get("aud") or data.get("azp") or ""
    if GOOGLE_CLIENT_ID:
        return aud == GOOGLE_CLIENT_ID or data.get("azp") == GOOGLE_CLIENT_ID
    return True


def _finish_oauth(*, email: str, full_name: str | None, photo: str | None, provider: str) -> dict:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=401, detail=f"{provider} did not share an email")
    try:
        user = User.from_oauth(email=email, full_name=full_name, photo=photo)
    except PersistError:
        user = None
    body_out = {
        "email": email,
        "full_name": full_name,
        "photo": photo or "",
        "provider": provider,
    }
    if user is not None:
        token_out = user.issue_token()
        body_out.update(user.public().model_dump())
        body_out["access_token"] = token_out.access_token
        body_out["token_type"] = "bearer"
        body_out["email"] = email
        body_out["photo"] = photo or user.photo or ""
        body_out["full_name"] = full_name or user.full_name
    return body_out


@router.post("/katie/auth/google")
def katie_auth_google(body: GoogleIn):
    id_token = (body.credential or "").strip()
    access = (body.access_token or "").strip()
    if not id_token and not access:
        raise HTTPException(status_code=400, detail="Missing Google credential")
    try:
        if id_token:
            data = _http_json(
                "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(id_token)
            )
        else:
            data = _http_json(
                "https://oauth2.googleapis.com/tokeninfo?access_token=" + urllib.parse.quote(access)
            )
            info = _http_json(
                "https://www.googleapis.com/oauth2/v3/userinfo?access_token=" + urllib.parse.quote(access)
            )
            data = {**data, **info}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Could not check Google sign-in") from exc
    iss = str(data.get("iss") or "")
    if iss and iss not in ("https://accounts.google.com", "accounts.google.com"):
        raise HTTPException(status_code=401, detail="Google sign-in did not match this shop")
    if not _google_ok_audience(data):
        raise HTTPException(status_code=401, detail="Google sign-in did not match this shop")
    verified = data.get("email_verified")
    if verified not in (True, "true", "True", None, ""):
        raise HTTPException(status_code=401, detail="Google email is not verified")
    return _finish_oauth(
        email=(data.get("email") or "").strip(),
        full_name=(data.get("name") or "").strip() or None,
        photo=(data.get("picture") or "").strip() or None,
        provider="google",
    )


@router.post("/katie/auth/apple")
def katie_auth_apple(body: AppleIn):
    token = (body.identity_token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing Apple token")
    email = (body.email or "").strip()
    try:
        import jwt
        from jwt import PyJWKClient

        jwks = PyJWKClient("https://appleid.apple.com/auth/keys")
        signing_key = jwks.get_signing_key_from_jwt(token)
        decode_kw = {
            "algorithms": ["RS256"],
            "issuer": "https://appleid.apple.com",
        }
        if APPLE_CLIENT_ID:
            decode_kw["audience"] = APPLE_CLIENT_ID
        payload = jwt.decode(token, signing_key.key, **decode_kw)
        email = email or (payload.get("email") or "")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Could not check Apple sign-in") from exc
    return _finish_oauth(
        email=email,
        full_name=(body.name or "").strip() or None,
        photo=None,
        provider="apple",
    )


@router.post("/katie/auth/facebook")
def katie_auth_facebook(body: FacebookIn):
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
    picture = None
    pic = data.get("picture")
    if isinstance(pic, dict):
        picture = ((pic.get("data") or {}).get("url") or "").strip() or None
    return _finish_oauth(
        email=(data.get("email") or "").strip(),
        full_name=(data.get("name") or "").strip() or None,
        photo=picture,
        provider="facebook",
    )
