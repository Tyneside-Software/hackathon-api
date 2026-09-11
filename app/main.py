#!/usr/bin/env python3
"""Hackathon API — FastAPI for Cloud Run."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

VERSION = "0.1.4"

app = FastAPI(title="Hackathon API", version=VERSION)


class FieldPayload(BaseModel):
    key: str
    value: str


class KeyPayload(BaseModel):
    key: str

_raw = os.getenv(
    "CORS_ORIGINS",
    "http://127.0.0.1:5500,http://localhost:5500,"
    "https://hackathon.tyneside.software,https://michaelthomsoncc.github.io",
)
origins = [o.strip() for o in _raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
    max_age=600,
)


@app.get("/")
def root() -> dict:
    return {
        "service": "hackathon-api",
        "docs": "/docs",
        "health": "/health",
        "test_field": "/test_field",
        "delete_field": "/delete_field",
        "version": VERSION,
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "hackathon-api",
        "utc": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
    }

@app.post("/create_field")
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


@app.get("/view_field/{key}")
def view_field(key: str) -> dict:
    """Retrieve a field value from Datastore by key."""
    from google.cloud import datastore

    client = datastore.Client()
    entity_key = client.key("Field", key)
    entity = client.get(entity_key)
    if entity is None:
        return {"ok": False, "message": "Field not found", "key": key}

    return {"ok": True, "key": key, "value": entity.get("value")}


def _delete_field_by_key(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "no key exists", "key": key}

    try:
        from google.cloud import datastore

        client = datastore.Client()
        entity_key = client.key("Field", key)
        entity = client.get(entity_key)
        if entity is None:
            return {"ok": False, "message": "no key exists", "key": key}

        client.delete(entity_key)
        return {"ok": True, "message": "Field deleted", "key": key}
    except Exception:
        # Unhandled 500s skip CORS headers and show up as a browser CORS error.
        return {"ok": False, "message": "no key exists", "key": key}


@app.post("/delete_field")
def delete_field(payload: KeyPayload) -> dict:
    """Delete a field from Datastore by key."""
    return _delete_field_by_key(payload.key)


@app.get("/test_field")
def test_field() -> dict:
    return {"ok": True, "key": "example_key", "value": "example_value"}


if __name__ == "__main__":
    import uvicorn

    # Pass the app object (not "app.main:app") so this works when the
    # Cloud Build trigger execs GOOGLE_ENTRYPOINT=app/main.py.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
