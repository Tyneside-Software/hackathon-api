"""Key/value field routes."""
from __future__ import annotations

from fastapi import APIRouter

from ..db import get_field, upsert_field
from ..schemas import FieldPayload

router = APIRouter()


@router.post("/create_field")
def create_field(payload: FieldPayload) -> dict:
    """Create or update a field in SQLite by key."""
    upsert_field(payload.key, payload.value)
    return {"ok": True, "message": "Field stored", "key": payload.key, "value": payload.value}


@router.get("/view_field/{key}")
def view_field(key: str) -> dict:
    """Retrieve a field value from SQLite by key."""
    value = get_field(key)
    if value is None:
        return {"ok": False, "message": "Field not found", "key": key}
    return {"ok": True, "key": key, "value": value}


@router.get("/test_field")
def test_field() -> dict:
    return {"ok": True, "key": "example_key", "value": "example_value"}
