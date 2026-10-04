"""SQLite access via SQLAlchemy."""
from __future__ import annotations

import json

from sqlalchemy import select

from .config import log
from .database import PersistError, SessionLocal, UserExistsError, normalise_username, utc_now
from .models import BusCache, Device, DeviceLink, FieldRecord, LocationPing

# Last decoded bus snapshot in this process. SQLite is only read on a miss
# or after an upstream refresh.
_bus_mem: dict[str, dict] = {}

__all__ = [
    "PersistError",
    "UserExistsError",
    "as_iso",
    "device_public",
    "get_device_row",
    "get_field",
    "link_device",
    "list_linked_devices",
    "set_device_nickname",
    "list_device_rows",
    "list_pings",
    "load_bus_cache",
    "normalise_username",
    "ping_public",
    "save_location",
    "upsert_field",
    "utc_now",
    "write_bus_cache",
]


def as_iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def device_public(row: dict) -> dict:
    return {
        "device_id": row.get("device_id"),
        "last_lat": row.get("last_lat"),
        "last_lng": row.get("last_lng"),
        "last_seen_at": row.get("last_seen_at"),
        "accuracy_m": row.get("accuracy_m"),
        "heading": row.get("heading"),
        "speed_mps": row.get("speed_mps"),
        "updated_at": row.get("updated_at"),
        "nickname": row.get("nickname"),
        "username": row.get("username"),
    }


def ping_public(row: dict) -> dict:
    return {
        "device_id": row.get("device_id"),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "accuracy_m": row.get("accuracy_m"),
        "heading": row.get("heading"),
        "speed_mps": row.get("speed_mps"),
        "recorded_at": as_iso(row.get("recorded_at")),
        "received_at": as_iso(row.get("received_at")),
    }


def _device_to_row(dev: Device) -> dict:
    return {
        "device_id": dev.device_id,
        "last_lat": dev.last_lat,
        "last_lng": dev.last_lng,
        "last_seen_at": dev.last_seen_at,
        "accuracy_m": dev.accuracy_m,
        "heading": dev.heading,
        "speed_mps": dev.speed_mps,
        "updated_at": dev.updated_at,
    }


def save_location(row: dict, ping: dict) -> str:
    with SessionLocal() as session:
        try:
            device_id = row["device_id"]
            dev = session.get(Device, device_id)
            if dev is None:
                dev = Device(device_id=device_id)
                session.add(dev)
            dev.last_lat = row["last_lat"]
            dev.last_lng = row["last_lng"]
            dev.last_seen_at = row.get("last_seen_at")
            dev.accuracy_m = row.get("accuracy_m")
            dev.heading = row.get("heading")
            dev.speed_mps = row.get("speed_mps")
            dev.updated_at = row.get("updated_at")
            session.add(
                LocationPing(
                    device_id=device_id,
                    lat=ping["lat"],
                    lng=ping["lng"],
                    accuracy_m=ping.get("accuracy_m"),
                    heading=ping.get("heading"),
                    speed_mps=ping.get("speed_mps"),
                    recorded_at=ping.get("recorded_at"),
                    received_at=ping.get("received_at"),
                )
            )
            session.commit()
            return "sqlite"
        except Exception as exc:
            session.rollback()
            log.warning("SQLite location write failed: %s", exc)
            raise PersistError("Could not store location") from exc


def _link_public(link: DeviceLink) -> dict:
    nick = (link.nickname or "").strip() or None
    return {
        "device_id": link.device_id,
        "username": link.username,
        "nickname": nick,
        "linked_at": link.linked_at,
    }


def list_device_rows() -> list[dict]:
    with SessionLocal() as session:
        devices = {d.device_id: d for d in session.scalars(select(Device)).all()}
        links = {l.device_id: l for l in session.scalars(select(DeviceLink)).all()}
        latest_ping = {}
        for p in session.scalars(select(LocationPing).order_by(LocationPing.recorded_at.desc())).all():
            if p.device_id not in latest_ping:
                latest_ping[p.device_id] = p
    out = []
    for did in set(devices) | set(latest_ping):
        d = devices.get(did)
        p = latest_ping.get(did)
        row = _device_to_row(d) if d else {"device_id": did}
        if p and isinstance(p.lat, (int, float)) and isinstance(p.lng, (int, float)):
            row["last_lat"] = p.lat
            row["last_lng"] = p.lng
            row["last_seen_at"] = p.recorded_at or row.get("last_seen_at")
            if p.accuracy_m is not None:
                row["accuracy_m"] = p.accuracy_m
        link = links.get(did)
        if link:
            row["nickname"] = (link.nickname or "").strip() or None
            row["username"] = link.username
        out.append(device_public(row))
    out.sort(key=lambda item: item.get("last_seen_at") or "", reverse=True)
    return out


def link_device(username: str, device_id: str, nickname: str | None = None) -> dict:
    device_id = (device_id or "").strip()
    username = normalise_username(username)
    nick = (nickname or "").strip() or None
    if nick and len(nick) > 64:
        nick = nick[:64]
    if not device_id or not username:
        raise PersistError("Could not link device")
    with SessionLocal() as session:
        existing = session.get(DeviceLink, device_id)
        if existing is not None and existing.username != username:
            raise UserExistsError("Device already linked to another account")
        now = utc_now()
        if existing is None:
            existing = DeviceLink(
                device_id=device_id,
                username=username,
                nickname=nick,
                linked_at=now,
            )
            session.add(existing)
        else:
            if nick is not None:
                existing.nickname = nick
        session.commit()
        return _link_public(existing)


def set_device_nickname(username: str, device_id: str, nickname: str) -> dict:
    device_id = (device_id or "").strip()
    username = normalise_username(username)
    nick = (nickname or "").strip()
    if not nick:
        raise PersistError("Nickname cannot be empty")
    if len(nick) > 64:
        nick = nick[:64]
    with SessionLocal() as session:
        existing = session.get(DeviceLink, device_id)
        if existing is None or existing.username != username:
            raise PersistError("Device is not linked to this account")
        existing.nickname = nick
        session.commit()
        return _link_public(existing)


def list_linked_devices(username: str) -> list[dict]:
    key = normalise_username(username)
    with SessionLocal() as session:
        rows = list(
            session.scalars(select(DeviceLink).where(DeviceLink.username == key)).all()
        )
    rows.sort(key=lambda r: r.linked_at or "", reverse=True)
    return [_link_public(r) for r in rows]


def get_device_row(device_id: str) -> dict | None:
    with SessionLocal() as session:
        dev = session.get(Device, device_id)
        if dev is None:
            return None
        row = _device_to_row(dev)
        link = session.get(DeviceLink, device_id)
        if link:
            row["nickname"] = (link.nickname or "").strip() or None
            row["username"] = link.username
        return device_public(row)


def list_pings(device_id: str, take: int) -> list[dict]:
    with SessionLocal() as session:
        stmt = (
            select(LocationPing)
            .where(LocationPing.device_id == device_id)
            .order_by(LocationPing.recorded_at.desc())
            .limit(take)
        )
        rows = list(session.scalars(stmt).all())
    return [
        ping_public(
            {
                "device_id": p.device_id,
                "lat": p.lat,
                "lng": p.lng,
                "accuracy_m": p.accuracy_m,
                "heading": p.heading,
                "speed_mps": p.speed_mps,
                "recorded_at": p.recorded_at,
                "received_at": p.received_at,
            }
        )
        for p in rows
    ]


def upsert_field(key: str, value: str) -> None:
    with SessionLocal() as session:
        rec = session.get(FieldRecord, key)
        if rec is None:
            rec = FieldRecord(key=key, value=value, updated_at=utc_now())
            session.add(rec)
        else:
            rec.value = value
            rec.updated_at = utc_now()
        session.commit()


def get_field(key: str) -> str | None:
    with SessionLocal() as session:
        rec = session.get(FieldRecord, key)
        if rec is None:
            return None
        return rec.value


def _bus_trails(data: dict) -> dict:
    trails = data.get("trails")
    if trails is None and data.get("trails_json"):
        try:
            trails = json.loads(data["trails_json"])
        except (TypeError, ValueError):
            trails = {}
    if not isinstance(trails, dict):
        return {}
    out = {}
    for key, pts in trails.items():
        if not isinstance(pts, list):
            continue
        clean = []
        for p in pts:
            if not isinstance(p, (list, tuple)) or len(p) < 3:
                continue
            try:
                clean.append([float(p[0]), float(p[1]), int(p[2])])
            except (TypeError, ValueError):
                continue
        if clean:
            out[str(key)] = clean
    return out


def _bus_row(data: dict, region: str) -> dict:
    vehicles = data.get("vehicles")
    if vehicles is None and data.get("vehicles_json"):
        try:
            vehicles = json.loads(data["vehicles_json"])
        except (TypeError, ValueError):
            vehicles = []
    if not isinstance(vehicles, list):
        vehicles = []
    return {
        "region": region,
        "fetched_at": as_iso(data.get("fetched_at")) or data.get("fetched_at"),
        "count": int(data.get("count") or len(vehicles)),
        "vehicles": vehicles,
        "trails": _bus_trails(data),
    }


def load_bus_cache(region: str) -> dict | None:
    key = (region or "").strip() or "newcastle"
    mem = _bus_mem.get(key)
    if mem is not None:
        return mem
    with SessionLocal() as session:
        rec = session.get(BusCache, key)
        if rec is None:
            return None
        row = _bus_row(
            {
                "fetched_at": rec.fetched_at,
                "count": rec.count,
                "vehicles_json": rec.vehicles_json,
                "trails_json": rec.trails_json,
            },
            key,
        )
    _bus_mem[key] = row
    return row


def write_bus_cache(region: str, row: dict) -> str:
    key = (region or "").strip() or "newcastle"
    stored = {
        "region": key,
        "fetched_at": row.get("fetched_at") or utc_now(),
        "count": int(row.get("count") or len(row.get("vehicles") or [])),
        "vehicles": list(row.get("vehicles") or []),
        "trails": dict(row.get("trails") or {}),
    }
    _bus_mem[key] = stored
    with SessionLocal() as session:
        rec = session.get(BusCache, key)
        if rec is None:
            rec = BusCache(region=key)
            session.add(rec)
        rec.fetched_at = stored["fetched_at"]
        rec.count = stored["count"]
        rec.vehicles_json = json.dumps(stored["vehicles"], separators=(",", ":"))
        rec.trails_json = json.dumps(stored["trails"], separators=(",", ":"))
        session.commit()
    return "sqlite"
