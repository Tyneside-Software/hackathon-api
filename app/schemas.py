"""Pydantic request bodies."""
from __future__ import annotations

from pydantic import BaseModel, Field


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
