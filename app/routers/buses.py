"""Shared live-bus cache.

The map never calls bustimes.org. Every looking tab hits GET /v1/buses.
If the Firestore snapshot is younger than BUS_TTL_S we serve it. If it is
stale (or missing) this request fetches bustimes.org once, writes Firestore,
and everyone else rides that snapshot. No looking tabs means no GET, which
means no upstream hit.
"""
from __future__ import annotations

import json
import math
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..config import (
    BUS_BBOX,
    BUS_CENTRE,
    BUS_MILES,
    BUS_REGION,
    BUS_TTL_S,
    BUS_UPSTREAM,
    VERSION,
    log,
)
from ..db import load_bus_cache, utc_now, write_bus_cache

router = APIRouter()
_refresh_lock = threading.Lock()
_UA = f"TynesideHackathon/{VERSION} (hackathon.tyneside.software)"


def _age_s(fetched_at: str | None) -> float | None:
    if not fetched_at:
        return None
    try:
        dt = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())


def _fresh(row: dict | None) -> bool:
    if not row or not row.get("vehicles"):
        return False
    age = _age_s(row.get("fetched_at"))
    return age is not None and age < BUS_TTL_S


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    h = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _slim(v: dict) -> dict | None:
    coords = v.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return None
    try:
        lng = float(coords[0])
        lat = float(coords[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lng):
        return None
    km = BUS_MILES * 1.609344
    if _haversine_km(BUS_CENTRE[0], BUS_CENTRE[1], lat, lng) > km:
        return None
    svc = v.get("service") if isinstance(v.get("service"), dict) else {}
    veh = v.get("vehicle") if isinstance(v.get("vehicle"), dict) else {}
    css = veh.get("css") or veh.get("colour")
    return {
        "id": v.get("id"),
        "coordinates": [lng, lat],
        "heading": v.get("heading"),
        "datetime": v.get("datetime"),
        "destination": v.get("destination") or "",
        "service": {
            "line_name": svc.get("line_name") or "",
            "url": svc.get("url") or "",
        },
        "vehicle": {
            "name": veh.get("name") or "",
            "css": css or "",
            "text_colour": veh.get("text_colour") or "",
        },
    }


def fetch_upstream() -> list[dict]:
    req = urllib.request.Request(
        BUS_UPSTREAM,
        headers={"Accept": "application/json", "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        return []
    out = []
    for v in data:
        if isinstance(v, dict):
            slim = _slim(v)
            if slim:
                out.append(slim)
    return out


def _payload(row: dict, source: str, refreshed: bool, stale: bool = False) -> dict:
    age = _age_s(row.get("fetched_at"))
    vehicles = list(row.get("vehicles") or [])
    return {
        "ok": True,
        "region": row.get("region") or BUS_REGION,
        "count": int(row.get("count") or len(vehicles)),
        "vehicles": vehicles,
        "fetched_at": row.get("fetched_at"),
        "age_s": None if age is None else round(age, 1),
        "ttl_s": BUS_TTL_S,
        "refreshed": refreshed,
        "stale": stale,
        "source": source,
        "bbox": BUS_BBOX,
    }


@router.get("/v1/buses")
def list_buses() -> dict:
    """Live vehicles inside 30 miles of Newcastle. Cached in Firestore."""
    cached = load_bus_cache(BUS_REGION)
    if _fresh(cached):
        return _payload(cached, "cache", False)

    with _refresh_lock:
        cached = load_bus_cache(BUS_REGION)
        if _fresh(cached):
            return _payload(cached, "cache", False)
        try:
            vehicles = fetch_upstream()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.warning("bustimes.org fetch failed: %s", exc)
            if cached and cached.get("vehicles"):
                body = _payload(cached, "cache", False, stale=True)
                body["message"] = "Upstream quiet — serving last snapshot."
                return body
            raise HTTPException(
                status_code=502,
                detail="Could not reach bustimes.org and no cache yet.",
            ) from exc
        row = {
            "region": BUS_REGION,
            "fetched_at": utc_now(),
            "count": len(vehicles),
            "vehicles": vehicles,
        }
        stored = write_bus_cache(BUS_REGION, row)
        source = stored if stored in ("firestore", "datastore") else "bustimes"
        return _payload(row, source, True)
