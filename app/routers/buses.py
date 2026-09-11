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
    BUS_TRAIL_S,
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


def _seed_trails(row: dict) -> dict:
    """Old snapshots have vehicles but no trails — start a point per bus without another upstream fetch."""
    if row.get("trails"):
        return row
    now_unix = int(datetime.now(timezone.utc).timestamp())
    row = dict(row)
    row["trails"] = merge_trails({}, row.get("vehicles") or [], now_unix)
    write_bus_cache(row.get("region") or BUS_REGION, row)
    return row


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


def _unix(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = int(value)
        if n > 10_000_000_000:
            n //= 1000
        return n
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def merge_trails(old: dict | None, vehicles: list[dict], now_unix: int) -> dict:
    """Keep up to BUS_TRAIL_S of [lng, lat, unix] per vehicle id."""
    cutoff = now_unix - BUS_TRAIL_S
    min_km = 0.012
    out: dict[str, list] = {}
    seen: set[str] = set()
    prev = old or {}
    for v in vehicles:
        vid = v.get("id")
        if vid is None:
            continue
        key = str(vid)
        seen.add(key)
        coords = v.get("coordinates") or []
        if len(coords) < 2:
            continue
        try:
            lng, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        t = _unix(v.get("datetime")) or now_unix
        pts = []
        for p in prev.get(key) or []:
            if not isinstance(p, (list, tuple)) or len(p) < 3:
                continue
            try:
                pu = int(p[2])
                plng, plat = float(p[0]), float(p[1])
            except (TypeError, ValueError):
                continue
            if pu >= cutoff:
                pts.append([plng, plat, pu])
        moved = True
        if pts:
            last = pts[-1]
            moved = _haversine_km(last[1], last[0], lat, lng) >= min_km or (t - last[2]) >= 12
        if moved:
            pts.append([lng, lat, t])
        if len(pts) > 50:
            pts = pts[-50:]
        if pts:
            out[key] = pts
    for key, raw in prev.items():
        if key in seen:
            continue
        pts = []
        for p in raw or []:
            if not isinstance(p, (list, tuple)) or len(p) < 3:
                continue
            try:
                pu = int(p[2])
                if pu >= cutoff:
                    pts.append([float(p[0]), float(p[1]), pu])
            except (TypeError, ValueError):
                continue
        if pts:
            out[str(key)] = pts
    return out


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
        "trails": dict(row.get("trails") or {}),
        "trail_s": BUS_TRAIL_S,
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
        return _payload(_seed_trails(cached), "cache", False)

    with _refresh_lock:
        cached = load_bus_cache(BUS_REGION)
        if _fresh(cached):
            return _payload(_seed_trails(cached), "cache", False)
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
        now_unix = int(datetime.now(timezone.utc).timestamp())
        trails = merge_trails((cached or {}).get("trails"), vehicles, now_unix)
        row = {
            "region": BUS_REGION,
            "fetched_at": utc_now(),
            "count": len(vehicles),
            "vehicles": vehicles,
            "trails": trails,
        }
        stored = write_bus_cache(BUS_REGION, row)
        source = stored if stored in ("firestore", "datastore") else "bustimes"
        return _payload(row, source, True)
