"""Pydantic request bodies."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Token(BaseModel):
    access_token: str
    token_type: str


class User(BaseModel):
    username: str
    email: str | None = None
    full_name: str | None = None
    disabled: bool | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None
    full_name: str | None = None


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
