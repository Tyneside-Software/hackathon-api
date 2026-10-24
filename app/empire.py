"""Account fleet. One game per username. Cars are never devices.

The clock starts from the FastAPI lifespan. Tests call tick_all with a
timestamp and never open the live database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.request
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import SessionLocal, normalise_username
from .models import EmpireCar, EmpireGame

log = logging.getLogger("hackathon-api")

DEPOT = (55.0367, -1.5075)
CIRCLE_CENTRE = (54.9783, -1.6178)
CIRCLE_RADIUS_KM = 80.467
EARTH_KM = 6371.0
KM_PER_MILE = 1.609344
MPH = 30.0
CHECK_S = 10
TRAIL_S = 600
DEPOT_PAUSE_S = 4
STAGGER_S = 7
PICKUP_MILES = 1.2
DROP_MILES = 3.0
BEARING_MIN = 160
BEARING_MAX = 300
NAMES = ("Mina", "Owen", "Priya", "Jack", "Helen", "Tom")
CARS = (
    ("T1", "#f5b942", 0),
    ("T2", "#38bdf8", STAGGER_S),
    ("T3", "#4ade80", STAGGER_S * 2),
)
STATUSES = ("at_depot", "to_passenger", "with_passenger", "returning")
BERWICK = (55.7692, -2.0084)

RouteFn = Callable[[tuple[float, float], tuple[float, float]], dict | None]
_route_fn: RouteFn | None = None
_fault: Callable[[EmpireCar], bool] | None = None
_clock: asyncio.Task | None = None


def set_route_fn(fn: RouteFn | None) -> None:
    """Tests pass a function. None restores the OSRM call, which tests do not use."""
    global _route_fn
    _route_fn = fn


def set_fault(fn: Callable[[EmpireCar], bool] | None) -> None:
    global _fault
    _fault = fn


def clock_running() -> bool:
    return _clock is not None and not _clock.done()


def start_clock() -> None:
    global _clock
    if clock_running():
        return
    _clock = asyncio.create_task(_clock_loop())


async def stop_clock() -> None:
    global _clock
    task = _clock
    _clock = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _clock_loop() -> None:
    while True:
        try:
            tick_all(time.time())
        except Exception:
            log.exception("empire tick failed")
        await asyncio.sleep(1)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1 = a
    lat2, lng2 = b
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    half = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 2 * EARTH_KM * math.asin(math.sqrt(min(1.0, half)))


def inside_circle(point: tuple[float, float]) -> bool:
    return haversine_km(CIRCLE_CENTRE, point) <= CIRCLE_RADIUS_KM


def destination(origin: tuple[float, float], bearing_deg: float, miles: float) -> tuple[float, float]:
    lat1 = math.radians(origin[0])
    lng1 = math.radians(origin[1])
    bearing = math.radians(bearing_deg)
    dist = miles * KM_PER_MILE / EARTH_KM
    lat2 = math.asin(
        math.sin(lat1) * math.cos(dist) + math.cos(lat1) * math.sin(dist) * math.cos(bearing)
    )
    lng2 = lng1 + math.atan2(
        math.sin(bearing) * math.sin(dist) * math.cos(lat1),
        math.cos(dist) - math.sin(lat1) * math.sin(lat2),
    )
    return (math.degrees(lat2), (math.degrees(lng2) + 540) % 360 - 180)


def raw_bearing(username: str) -> int:
    total = 0
    for ch in normalise_username(username):
        total = (total * 33 + ord(ch)) & 0xFFFFFFFF
    return total % 360


def trip_bearing(username: str) -> int:
    raw = raw_bearing(username)
    if raw < BEARING_MIN:
        return BEARING_MIN
    if raw > BEARING_MAX:
        return BEARING_MAX
    return raw


def _loads(raw: str, fallback: list) -> list:
    try:
        got = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return list(fallback)
    return got if isinstance(got, list) else list(fallback)


def _straight(start: tuple[float, float], end: tuple[float, float]) -> tuple[list, float, bool]:
    miles = haversine_km(start, end) / KM_PER_MILE
    if miles < 0.001:
        miles = 0.001
    return [[start[0], start[1]], [end[0], end[1]]], miles / MPH * 3600.0, False


def ask_osrm(start: tuple[float, float], end: tuple[float, float]) -> dict | None:
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{start[1]},{start[0]};{end[1]},{end[0]}?overview=full&geometries=geojson"
    )
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    routes = payload.get("routes") or []
    if not routes:
        return None
    coords = (routes[0].get("geometry") or {}).get("coordinates") or []
    points = [[lat, lng] for lng, lat in coords if isinstance(lat, (int, float)) and isinstance(lng, (int, float))]
    if len(points) < 2:
        return None
    duration = routes[0].get("duration")
    return {"points": points, "duration_s": float(duration) if duration else None}


def build_route(start: tuple[float, float], end: tuple[float, float]) -> tuple[list, float, bool]:
    fn = _route_fn if _route_fn is not None else ask_osrm
    try:
        got = fn(start, end)
    except Exception:
        log.exception("empire route failed")
        got = None
    if not got or not got.get("points"):
        return _straight(start, end)
    points = got["points"]
    duration = got.get("duration_s")
    if not duration or duration <= 0:
        miles = 0.0
        for i in range(len(points) - 1):
            miles += haversine_km(tuple(points[i]), tuple(points[i + 1])) / KM_PER_MILE
        duration = max(miles, 0.001) / MPH * 3600.0
    return points, float(duration), True


def point_at(route: list, fraction: float) -> tuple[float, float]:
    if not route:
        return DEPOT
    fraction = min(1.0, max(0.0, fraction))
    if len(route) == 1 or fraction <= 0:
        return float(route[0][0]), float(route[0][1])
    if fraction >= 1:
        return float(route[-1][0]), float(route[-1][1])
    lengths = []
    total = 0.0
    for i in range(len(route) - 1):
        dist = haversine_km((route[i][0], route[i][1]), (route[i + 1][0], route[i + 1][1]))
        lengths.append(dist)
        total += dist
    if total <= 0:
        return float(route[0][0]), float(route[0][1])
    target = total * fraction
    walked = 0.0
    for i, dist in enumerate(lengths):
        if walked + dist >= target:
            t = 0.0 if dist == 0 else (target - walked) / dist
            lat = route[i][0] + (route[i + 1][0] - route[i][0]) * t
            lng = route[i][1] + (route[i + 1][1] - route[i][1]) * t
            return float(lat), float(lng)
        walked += dist
    return float(route[-1][0]), float(route[-1][1])


def _cars(session: Session, username: str) -> list[EmpireCar]:
    return list(
        session.scalars(select(EmpireCar).where(EmpireCar.username == username).order_by(EmpireCar.sign)).all()
    )


def ensure_game(username: str, now: float | None = None, session: Session | None = None) -> EmpireGame:
    """Create T1, T2, and T3 at the depot when this account has no game yet."""
    username = normalise_username(username)
    own_session = session is None
    if session is None:
        session = SessionLocal()
    if now is None:
        now = time.time()
    try:
        game = session.get(EmpireGame, username)
        if game is None:
            game = EmpireGame(username=username, created_at=str(int(now)), name_index=0)
            session.add(game)
            for sign, colour, offset in CARS:
                session.add(
                    EmpireCar(
                        username=username,
                        sign=sign,
                        colour=colour,
                        status="at_depot",
                        passenger="",
                        lat=DEPOT[0],
                        lng=DEPOT[1],
                        route_json="[]",
                        duration_s=0,
                        leg_started=now,
                        osrm=False,
                        trail_json="[]",
                        fare_name="",
                        hold_until=now + offset,
                        next_check=now + CHECK_S,
                        depart_offset=offset,
                    )
                )
            session.commit()
        elif not _cars(session, username):
            for sign, colour, offset in CARS:
                session.add(
                    EmpireCar(
                        username=username,
                        sign=sign,
                        colour=colour,
                        status="at_depot",
                        lat=DEPOT[0],
                        lng=DEPOT[1],
                        hold_until=now + offset,
                        next_check=now + CHECK_S,
                        depart_offset=offset,
                    )
                )
            session.commit()
        return game
    finally:
        if own_session:
            session.close()


def _next_name(game: EmpireGame) -> str:
    name = NAMES[game.name_index % len(NAMES)]
    game.name_index = game.name_index + 1
    return name


def _begin(car: EmpireCar, game: EmpireGame, start: tuple[float, float], end: tuple[float, float], now: float, status: str) -> None:
    route, duration, osrm = build_route(start, end)
    if status == "to_passenger":
        if not route:
            route = [[DEPOT[0], DEPOT[1]]]
        route[0] = [DEPOT[0], DEPOT[1]]
    car.status = status
    car.route_json = json.dumps(route)
    car.duration_s = max(float(duration), 0.001)
    car.leg_started = now
    car.osrm = osrm
    car.lat, car.lng = point_at(route, 0)


def _start_fare(car: EmpireCar, game: EmpireGame, now: float) -> None:
    bearing = trip_bearing(car.username)
    pickup = destination(DEPOT, bearing, PICKUP_MILES)
    drop = destination(DEPOT, bearing, DROP_MILES)
    name = _next_name(game)
    car.passenger = name
    car.fare_name = name
    car.fare_lat, car.fare_lng = pickup
    car.drop_lat, car.drop_lng = drop
    _begin(car, game, DEPOT, pickup, now, "to_passenger")


def _finish_leg(car: EmpireCar, game: EmpireGame, now: float) -> None:
    if car.status == "to_passenger":
        start = (car.fare_lat, car.fare_lng)
        end = (car.drop_lat, car.drop_lng)
        car.fare_name = ""
        _begin(car, game, start, end, now, "with_passenger")
        return
    if car.status == "with_passenger":
        start = (car.drop_lat, car.drop_lng)
        car.passenger = ""
        _begin(car, game, start, DEPOT, now, "returning")
        return
    if car.status == "returning":
        car.status = "at_depot"
        car.passenger = ""
        car.fare_name = ""
        car.lat, car.lng = DEPOT
        car.route_json = json.dumps([list(DEPOT), list(DEPOT)])
        car.duration_s = 0
        car.leg_started = now
        car.osrm = True
        car.hold_until = now + DEPOT_PAUSE_S


def _check_in(car: EmpireCar, now: float) -> None:
    trail = _loads(car.trail_json, [])
    while now + 1e-6 >= car.next_check:
        trail.append([round(float(car.lng), 6), round(float(car.lat), 6), int(car.next_check)])
        car.next_check += CHECK_S
    cutoff = int(now) - TRAIL_S
    trail = [point for point in trail if isinstance(point, list) and len(point) >= 3 and int(point[2]) >= cutoff]
    car.trail_json = json.dumps(trail)


def advance_car(car: EmpireCar, game: EmpireGame, now: float) -> None:
    if _fault is not None and _fault(car):
        raise RuntimeError("empire car fault")
    for _ in range(48):
        if car.status == "at_depot":
            car.lat, car.lng = DEPOT
            if now < car.hold_until:
                break
            _start_fare(car, game, car.hold_until)
            continue
        elapsed = now - car.leg_started
        route = _loads(car.route_json, [])
        if car.duration_s <= 0 or elapsed < car.duration_s:
            fraction = 0.0 if car.duration_s <= 0 else elapsed / car.duration_s
            if route:
                car.lat, car.lng = point_at(route, fraction)
            break
        _finish_leg(car, game, car.leg_started + car.duration_s)
    _check_in(car, now)


def tick_all(now: float | None = None, session: Session | None = None) -> None:
    if now is None:
        now = time.time()
    own_session = session is None
    if session is None:
        session = SessionLocal()
    try:
        games = list(session.scalars(select(EmpireGame)).all())
        for game in games:
            for car in _cars(session, game.username):
                try:
                    advance_car(car, game, now)
                except Exception:
                    log.exception("empire car %s %s failed", car.username, car.sign)
        session.commit()
    finally:
        if own_session:
            session.close()


def snapshot(username: str, now: float | None = None, session: Session | None = None) -> dict:
    username = normalise_username(username)
    if now is None:
        now = time.time()
    own_session = session is None
    if session is None:
        session = SessionLocal()
    try:
        ensure_game(username, now, session)
        cars = _cars(session, username)
        moving = [car for car in cars if car.status != "at_depot"]
        unavailable = any(not car.osrm for car in moving)
        payload_cars = []
        fares = []
        for car in cars:
            if car.status not in STATUSES:
                car.status = "at_depot"
            route = _loads(car.route_json, [])
            trail = _loads(car.trail_json, [])
            payload_cars.append(
                {
                    "sign": car.sign,
                    "colour": car.colour,
                    "status": car.status,
                    "passenger": car.passenger,
                    "lat": car.lat,
                    "lng": car.lng,
                    "route": route,
                    "duration_s": car.duration_s,
                    "leg_started": car.leg_started,
                    "osrm": bool(car.osrm),
                    "trail": trail,
                }
            )
            if car.status == "to_passenger" and car.fare_name:
                fares.append(
                    {
                        "name": car.fare_name,
                        "lat": car.fare_lat,
                        "lng": car.fare_lng,
                        "sign": car.sign,
                    }
                )
        return {
            "ok": True,
            "username": username,
            "server_now": int(now),
            "check_s": CHECK_S,
            "trail_s": TRAIL_S,
            "osrm_unavailable": unavailable,
            "cars": payload_cars,
            "fares": fares,
        }
    finally:
        if own_session:
            session.close()


def game_count(username: str, session: Session) -> int:
    return int(
        session.scalar(select(func.count()).select_from(EmpireGame).where(EmpireGame.username == username)) or 0
    )
