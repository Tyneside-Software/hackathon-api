"""Hackathon API — FastAPI for Cloud Run."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

VERSION = "0.1.2"

app = FastAPI(title="Hackathon API", version=VERSION)

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
    allow_methods=["GET", "POST", "OPTIONS"],
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
def create_field(key, value) -> dict:
    """Create a new field in the firestore database called hackathon-firestore."""
    from google.cloud import datastore

    client = datastore.Client()
    key = client.key("Field")
    entity = datastore.Entity(key=key)
    entity.update({
        "name": "New Field",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    client.put(entity)
    return {"ok": True, "message": "Field created", "field_id": entity.key.id}

@app.get("/test_field")
def test_field() -> dict:
    return {"ok": True, "key": "example_key", "value": "example_value"}