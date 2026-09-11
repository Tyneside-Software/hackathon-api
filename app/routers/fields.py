"""Generic Datastore field routes."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from ..schemas import FieldPayload

router = APIRouter()


@router.post("/create_field")
def create_field(payload: FieldPayload) -> dict:
    """Create or update a field in Datastore by key."""
    from google.cloud import datastore

    client = datastore.Client()
    entity_key = client.key("Field", payload.key)
    entity = datastore.Entity(key=entity_key)
    entity.update(
        {
            "value": payload.value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    client.put(entity)

    return {"ok": True, "message": "Field stored", "key": payload.key, "value": payload.value}


@router.get("/view_field/{key}")
def view_field(key: str) -> dict:
    """Retrieve a field value from Datastore by key."""
    from google.cloud import datastore

    client = datastore.Client()
    entity_key = client.key("Field", key)
    entity = client.get(entity_key)
    if entity is None:
        return {"ok": False, "message": "Field not found", "key": key}

    return {"ok": True, "key": key, "value": entity.get("value")}


@router.get("/test_field")
def test_field() -> dict:
    return {"ok": True, "key": "example_key", "value": "example_value"}
