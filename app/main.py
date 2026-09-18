#!/usr/bin/env python3
"""Hackathon API — FastAPI for Cloud Run."""
from __future__ import annotations

import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import VERSION, origins
from app.database import init_db
from app.routers import auth, buses, devices, fields, health, locations


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Hackathon API", version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "Authorization"],
    max_age=600,
)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(fields.router)
app.include_router(locations.router)
app.include_router(devices.router)
app.include_router(buses.router)
init_db()


if __name__ == "__main__":
    import uvicorn

    # Pass the app object (not "app.main:app") so this works when the
    # Cloud Build trigger execs GOOGLE_ENTRYPOINT=app/main.py.
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
    )
