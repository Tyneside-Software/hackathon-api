"""Pydantic request bodies."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

USERNAME_CHARS = re.compile(r"[A-Za-z0-9._-]+")
USERNAME_HINT = (
    "Usernames can only use letters, numbers, dots, underscores and hyphens — no spaces."
)


def _check_username(value: str) -> str:
    value = (value or "").strip()
    if not USERNAME_CHARS.fullmatch(value):
        raise ValueError(USERNAME_HINT)
    return value


class Token(BaseModel):
    access_token: str
    token_type: str


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    photo: str | None = None
    admin: bool = False
    disabled: bool | None = None


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=64)
    photo: str | None = None
    full_name: str | None = None

    @field_validator("username")
    @classmethod
    def username_chars(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _check_username(value)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None
    full_name: str | None = None

    @field_validator("username")
    @classmethod
    def username_chars(cls, value: str) -> str:
        return _check_username(value)


class LoginRequest(BaseModel):
    username: str
    password: str


class FieldPayload(BaseModel):
    key: str
    value: str


class LocationPayload(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float | None = None
    recorded_at: str | None = None
    heading: float | None = None
    speed_mps: float | None = None
