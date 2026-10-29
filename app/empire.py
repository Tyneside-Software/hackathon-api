"""Account fleet. One game per username. Cars are never devices.

The clock starts from the FastAPI lifespan. Tests call tick_all with a
timestamp and never open the live database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from .database import SessionLocal, normalise_username
from .models import EmpireCar, EmpireGame, EmpirePerson

log = logging.getLogger("hackathon-api")

DEPOT = (55.0367, -1.5075)
EMPTY_MOVE_MILES = 2
EMPTY_MOVE_KM = EMPTY_MOVE_MILES * 1.609344
CIRCLE_CENTRE = (54.9783, -1.6178)
CIRCLE_RADIUS_KM = 80.467
LOITER_MIN_KM = 0.2
LOITER_MAX_KM = 8.0
EARTH_KM = 6371.0
KM_PER_MILE = 1.609344
MPH = 30.0
STANDING_PENCE = 1500
FUEL_PENCE_PER_MILE = 20
NEW_CAR_PENCE = 800000
CHECK_S = 10
TRAIL_S = 600
DEPOT_PAUSE_S = 4
BREAK_S = 180
STAGGER_S = 7
PICKUP_MILES = 1.2
DROP_MILES = 3.0
BEARING_MIN = 160
BEARING_MAX = 300
NAMES = ("Mina", "Owen", "Priya", "Jack", "Helen", "Tom")
CARS = tuple(
    (f"T{index}", colour, STAGGER_S * (index - 1))
    for index, colour in enumerate(
        (
            "#f5b942",
            "#38bdf8",
            "#4ade80",
            "#c084fc",
            "#fb7185",
            "#f97316",
            "#a3e635",
            "#22d3ee",
            "#e879f9",
            "#facc15",
        ),
        start=1,
    )
)
OPENING_SIGNS = frozenset(sign for sign, _colour, _offset in CARS)
FIRM_NAMES = ("north", "south")
FIRM_PREFIX = {"north": "N", "south": "S"}
FIRM_BOOKINGS_KEPT = 12
# One shared passenger queue. The key is not a username the register form accepts.
WORLD = "*world"
RESET_FLAG = "shared-20261029"
SHARED_HOUSES = {
    "michael": {"company": "Shiremoor Taxis", "prefix": "T", "depot": "shiremoor"},
    "noah": {"company": "North Taxis", "prefix": "N", "depot": "north"},
    "reeve": {"company": "South Taxis", "prefix": "S", "depot": "south"},
}
STATUSES = ("at_depot", "to_passenger", "with_passenger", "returning", "to_better_spot")
JOBS = ("waiting", "travelling to a better spot", "collecting", "carrying", "returning", "on break")
BERWICK = (55.7692, -2.0084)
# Published positions. Reading this table does not call the network.
PLACES = (
    ("Shiremoor", 55.03535, -1.50950),
    ("Backworth", 55.0333, -1.5167),
    ("Earsdon", 55.0464, -1.4783),
    ("West Monkseaton", 55.03782, -1.47107),
    ("Forest Hall", 55.0167, -1.5667),
    ("Killingworth", 55.0333, -1.5667),
    ("Benton", 55.0074, -1.5558),
    ("Cobalt", 55.0265, -1.4885),
    ("Silverlink", 55.0136, -1.4772),
    ("Royal Quays", 55.0042, -1.4635),
    ("Regent Centre", 55.0117, -1.6240),
    ("Four Lane Ends", 55.0101, -1.5785),
    ("Whitley Bay", 55.0508, -1.4539),
    ("Spanish City", 55.047616, -1.44747),
    ("Cullercoats", 55.031646, -1.440003),
    ("Tynemouth", 55.0206, -1.4494),
    ("North Shields", 55.0047, -1.4728),
    ("Grey's Monument", 54.9783, -1.6178),
    ("Central Station", 54.9686, -1.6171),
    ("Haymarket", 54.9776, -1.6139),
    ("the Quayside", 54.970172, -1.600184),
    ("Osborne Road", 54.9886, -1.6047),
    ("North Tyneside General at Rake Lane", 55.025, -1.467),
    ("the RVI", 54.981, -1.620),
    ("the Freeman", 55.002, -1.594),
    ("Newcastle Airport", 55.0371, -1.7106),
)

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


def _fleet_cars(session: Session, username: str) -> list[EmpireCar]:
    rows = list(session.scalars(select(EmpireCar).where(EmpireCar.username == username)).all())
    return sorted(rows, key=lambda car: int(car.sign[1:]))


def _cars(session: Session, username: str) -> list[EmpireCar]:
    """The account's own cars. Another firm's cars are not part of this list."""
    return [car for car in _fleet_cars(session, username) if not (car.firm or "")]


def ensure_game(username: str, now: float | None = None, session: Session | None = None) -> EmpireGame:
    """Create T1 through T10 at the depot when this account has no game yet."""
    username = normalise_username(username)
    own_session = session is None
    if session is None:
        session = SessionLocal()
    if now is None:
        now = time.time()
    try:
        game = session.get(EmpireGame, username)
        if game is None:
            house = SHARED_HOUSES.get(username)
            game = EmpireGame(
                username=username,
                created_at=str(int(now)),
                name_index=0,
                cash_pence=0,
                fare_in=0,
                miles_driven=0,
                firm_bookings="[]",
                trip_bookings="[]",
                company_name=house["company"] if house else "",
            )
            session.add(game)
            _add_opening_cars(session, username, now)
            if username in SHARED_HOUSES:
                _ensure_world(session, now)
            else:
                ensure_firms(username, now, session)
            session.commit()
        elif not _cars(session, username):
            _add_opening_cars(session, username, now)
            if username in SHARED_HOUSES:
                _ensure_world(session, now)
                _remember_company(game)
            else:
                ensure_firms(username, now, session)
            session.commit()
        else:
            dirty = False
            if username in SHARED_HOUSES:
                dirty = _ensure_world(session, now) or _remember_company(game)
            elif ensure_firms(username, now, session):
                dirty = True
            if dirty:
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
    """Start the leg where the car already is. Do not snap the start back to Shiremoor."""
    route, duration, osrm = build_route(start, end)
    if not route:
        route = [[start[0], start[1]], [end[0], end[1]]]
    route[0] = [float(start[0]), float(start[1])]
    car.status = status
    car.route_json = json.dumps(route)
    car.duration_s = max(float(duration), 0.001)
    car.leg_started = now
    car.osrm = osrm
    car.lat, car.lng = point_at(route, 0)


HOMES = (
    "Shiremoor",
    "Backworth",
    "Earsdon",
    "West Monkseaton",
    "Forest Hall",
    "Killingworth",
    "Benton",
)
MORNING_DROPS = (
    "Cobalt",
    "North Tyneside General at Rake Lane",
    "Regent Centre",
    "Grey's Monument",
    "Central Station",
    "Haymarket",
    "the Quayside",
    "Osborne Road",
)
DAY_PLACES = (
    "Silverlink",
    "Royal Quays",
    "Whitley Bay",
    "Spanish City",
    "Cullercoats",
    "Tynemouth",
    "North Shields",
    "North Tyneside General at Rake Lane",
    "the RVI",
    "the Freeman",
    "Grey's Monument",
    "Central Station",
)
COAST = (
    "Whitley Bay",
    "Spanish City",
    "Cullercoats",
    "Tynemouth",
    "North Shields",
)
TOWN = (
    "Grey's Monument",
    "Central Station",
    "Haymarket",
    "the Quayside",
    "Osborne Road",
)
EVENING_PICKUPS = ("Cobalt",) + TOWN
EVENING_DROPS = HOMES + COAST
NIGHT_PICKUPS = (
    "the Quayside",
    "Osborne Road",
    "Tynemouth",
    "Whitley Bay",
)
AIRPORT_NAME = "Newcastle Airport"
# Relative weights for places people live. Not a new town, and not a new hour.
HOME_WEIGHTS = {
    "Killingworth": 5,
    "Forest Hall": 4,
    "Benton": 4,
    "Shiremoor": 3,
    "West Monkseaton": 3,
    "Backworth": 2,
    "Earsdon": 2,
}
HOSPITALS = (
    "North Tyneside General at Rake Lane",
    "the RVI",
    "the Freeman",
)
# One table for every kind of journey. Weights are morning, day, evening, night, and the small hours.
# A zero weight means that kind is not drawn in that band. 800000 is not a weight.
JOURNEY_KINDS = (
    ("commute", HOMES, MORNING_DROPS, (8, 2, 6, 0, 1)),
    ("night out", NIGHT_PICKUPS, HOMES, (0, 1, 4, 8, 1)),
    ("hospital", HOMES, HOSPITALS, (2, 3, 2, 2, 3)),
    ("airport", (AIRPORT_NAME,), TOWN + COAST, (2, 2, 2, 3, 2)),
    ("coast", HOMES, COAST, (1, 5, 5, 1, 1)),
    ("short hop", HOMES, HOMES, (3, 4, 3, 2, 2)),
)


def place_point(name: str) -> tuple[float, float]:
    for place_name, lat, lng in PLACES:
        if place_name == name:
            return (lat, lng)
    raise KeyError(name)


def place_name(point: tuple[float, float]) -> str:
    """The published place this coordinate already is. No second table."""
    key = (round(float(point[0]), 5), round(float(point[1]), 5))
    for name, lat, lng in PLACES:
        if (round(lat, 5), round(lng, 5)) == key:
            return name
    if haversine_km((float(point[0]), float(point[1])), DEPOT) <= 0.3:
        return "Shiremoor"
    return ""


def _last_sunday(year: int, month: int) -> datetime:
    if month == 12:
        day = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    else:
        day = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    while day.weekday() != 6:
        day -= timedelta(days=1)
    return day


def _london_offset_hours(utc: datetime) -> int:
    """BST is UTC+1 from the last Sunday in March at 01:00 UTC until the last Sunday in October at 01:00 UTC."""
    start = _last_sunday(utc.year, 3).replace(hour=1)
    end = _last_sunday(utc.year, 10).replace(hour=1)
    if start <= utc < end:
        return 1
    return 0


def london_clock(now: float) -> tuple[int, int]:
    """Hour and minute in Europe/London. Not the machine zone."""
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    local = utc + timedelta(hours=_london_offset_hours(utc))
    return local.hour, local.minute


def is_morning(now: float) -> bool:
    hour, minute = london_clock(now)
    minutes = hour * 60 + minute
    return 7 * 60 <= minutes < 9 * 60 + 30


def is_day(now: float) -> bool:
    hour, minute = london_clock(now)
    minutes = hour * 60 + minute
    return 9 * 60 + 30 <= minutes < 16 * 60 + 30


def is_evening(now: float) -> bool:
    hour, minute = london_clock(now)
    minutes = hour * 60 + minute
    return 16 * 60 + 30 <= minutes < 19 * 60 + 30


def is_night(now: float) -> bool:
    hour, minute = london_clock(now)
    minutes = hour * 60 + minute
    return minutes >= 19 * 60 + 30 or minutes < 60


def journey_ends(username: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """Pickup and drop are two places. They are not a ring around the depot."""
    index = raw_bearing(username) % len(PLACES)
    pickup = PLACES[index]
    drop = PLACES[(index + 11) % len(PLACES)]
    return (pickup[1], pickup[2]), (drop[1], drop[2])


def morning_ends(username: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """A home toward Cobalt, Rake Lane, Regent Centre, or town."""
    index = raw_bearing(username)
    return place_point(HOMES[index % len(HOMES)]), place_point(MORNING_DROPS[index % len(MORNING_DROPS)])


def day_ends(username: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """Silverlink, Royal Quays, the coast, the hospitals, Grey's Monument, and Central Station."""
    index = raw_bearing(username) % len(DAY_PLACES)
    return place_point(DAY_PLACES[index]), place_point(DAY_PLACES[(index + 5) % len(DAY_PLACES)])


def evening_ends(username: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """Cobalt and town back to the homes and the coast."""
    index = raw_bearing(username)
    return (
        place_point(EVENING_PICKUPS[index % len(EVENING_PICKUPS)]),
        place_point(EVENING_DROPS[index % len(EVENING_DROPS)]),
    )


def night_ends(username: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """The Quayside, Osborne Road, Tynemouth, or Whitley Bay back to a home."""
    index = raw_bearing(username)
    return (
        place_point(NIGHT_PICKUPS[index % len(NIGHT_PICKUPS)]),
        place_point(HOMES[index % len(HOMES)]),
    )


def airport_sign(sign: str) -> bool:
    """One car in ten. T10, not the whole fleet."""
    return int(sign[1:]) % 10 == 0


def airport_ends(username: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """Newcastle Airport toward town or the coast."""
    index = raw_bearing(username)
    drops = TOWN + COAST
    return place_point(AIRPORT_NAME), place_point(drops[index % len(drops)])


def hour_ends(username: str, now: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Pickup and drop for this hour. The same ends the passenger trip already uses."""
    if is_morning(now):
        return morning_ends(username)
    if is_day(now):
        return day_ends(username)
    if is_evening(now):
        return evening_ends(username)
    if is_night(now):
        return night_ends(username)
    return journey_ends(username)


def hour_pickup(username: str, now: float) -> tuple[float, float]:
    """The place where a person waits. Not a ring around the depot."""
    if is_morning(now):
        pickup, _drop = morning_ends(username)
    elif is_day(now):
        pickup, _drop = day_ends(username)
    elif is_evening(now):
        pickup, _drop = evening_ends(username)
    elif is_night(now):
        pickup, _drop = night_ends(username)
    else:
        pickup, _drop = journey_ends(username)
    return pickup


def busy_spot(username: str, now: float) -> tuple[float, float]:
    """Where this hour is busy. A real place, not a ring around the depot."""
    if is_morning(now):
        _home, busy = morning_ends(username)
    elif is_day(now):
        busy, _other = day_ends(username)
    elif is_evening(now):
        _work, busy = evening_ends(username)
    elif is_night(now):
        busy, _home = night_ends(username)
    else:
        busy, _other = journey_ends(username)
    return busy


def _band_index(now: float) -> int:
    """0 morning, 1 day, 2 evening, 3 night, 4 the small hours. No new hour band."""
    if is_morning(now):
        return 0
    if is_day(now):
        return 1
    if is_evening(now):
        return 2
    if is_night(now):
        return 3
    return 4


def _engine_seed(username: str, key: str) -> int:
    total = 0
    for ch in f"{normalise_username(username)}|{key}":
        total = (total * 33 + ord(ch)) & 0xFFFFFFFF
    return total


def _spell_key(now: float) -> str:
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    local = utc + timedelta(hours=_london_offset_hours(utc))
    return f"{local.date().isoformat()}|{_band_index(now)}"


def spell_target(username: str, now: float) -> int:
    """How many people this spell draws.

    A quiet spell can be zero. A morning or evening spell is busier than the ten taxis.
    """
    band = _band_index(now)
    if band == 3:
        return 1
    rng = random.Random(_engine_seed(username, _spell_key(now) + "|target"))
    if band == 0:
        return 11 + rng.randrange(0, 4)
    if band == 1:
        return 1 + rng.randrange(0, 3)
    if band == 2:
        return 11 + rng.randrange(0, 3)
    return rng.choices((0, 1), weights=(3, 1))[0]


def _pick_name(rng: random.Random, names: tuple[str, ...]) -> str:
    weights = [HOME_WEIGHTS.get(name, 1) for name in names]
    return rng.choices(list(names), weights=weights, k=1)[0]


def draw_journey(username: str, now: float, salt: str) -> tuple[str, tuple[float, float], tuple[float, float]]:
    """Draw one journey. The hour changes the weights, not the function."""
    rng = random.Random(_engine_seed(username, f"{_spell_key(now)}|{salt}|journey"))
    band = _band_index(now)
    weights = [kind[3][band] for kind in JOURNEY_KINDS]
    if sum(weights) <= 0:
        weights = [1] * len(JOURNEY_KINDS)
    kind_name, pickups, drops, _weights = rng.choices(list(JOURNEY_KINDS), weights=weights, k=1)[0]
    pickup_name = _pick_name(rng, pickups)
    drop_name = _pick_name(rng, drops)
    for _ in range(8):
        if drop_name != pickup_name:
            break
        drop_name = _pick_name(rng, drops)
    if drop_name == pickup_name:
        for name in drops:
            if name != pickup_name:
                drop_name = name
                break
    if kind_name == "commute" and band == 2:
        pickup_name, drop_name = drop_name, pickup_name
    return kind_name, place_point(pickup_name), place_point(drop_name)


def _point_key(point: tuple[float, float]) -> tuple[float, float]:
    return (round(point[0], 5), round(point[1], 5))


def _point_taken(point: tuple[float, float], taken: tuple[tuple[float, float], ...]) -> bool:
    """True when this point is already one a car or a person is using."""
    key = _point_key(point)
    for other in taken:
        if _point_key(other) == key or haversine_km(point, other) <= 0.05:
            return True
    return False


def _inside_loiter(point: tuple[float, float], centre: tuple[float, float], radius_km: float) -> bool:
    return inside_circle(point) and haversine_km(point, centre) <= radius_km + 1e-4


def stored_loiter(game: EmpireGame) -> tuple[str, tuple[float, float], float] | None:
    """The patch the player set, or nothing when it has not been set."""
    radius = float(game.loiter_km or 0)
    if radius <= 0:
        return None
    firm = (game.loiter_firm or "").strip().lower()
    return firm, (float(game.loiter_lat), float(game.loiter_lng)), radius


def loiter_for_fleet(
    game: EmpireGame,
    draw_as: str | None,
) -> tuple[tuple[float, float], float] | None:
    """The patch for this fleet. Another firm's patch does not move these cars."""
    stored = stored_loiter(game)
    if stored is None:
        return None
    firm, centre, radius = stored
    if draw_as is None:
        if firm == "":
            return centre, radius
        return None
    if firm in FIRM_NAMES and draw_as == firm_key(game.username, firm):
        return centre, radius
    return None


def loiter_spot(
    username: str,
    sign: str,
    now: float,
    centre: tuple[float, float],
    radius_km: float,
    taken: tuple[tuple[float, float], ...] = (),
) -> tuple[float, float]:
    """One seeded point inside the patch. A taken point uses the next draw."""
    last = centre
    for attempt in range(24):
        salt = f"loiter-{sign}" if attempt == 0 else f"loiter-{sign}-{attempt}"
        rng = random.Random(_engine_seed(username, f"{_spell_key(now)}|{salt}"))
        bearing = rng.random() * 360.0
        miles = (radius_km * 0.98 * math.sqrt(rng.random())) / KM_PER_MILE
        if miles < 0.02:
            miles = 0.02
        point = destination(centre, bearing, miles)
        last = point
        if not _inside_loiter(point, centre, radius_km) or _point_taken(point, taken):
            continue
        return point
    number = int(sign[1:]) if len(sign) > 1 and sign[1:].isdigit() else 1
    for step in range(36):
        bearing = float((step * 37 + number * 17) % 360)
        miles = max(0.02, (radius_km * 0.55) / KM_PER_MILE)
        point = destination(centre, bearing, miles)
        last = point
        if _inside_loiter(point, centre, radius_km) and not _point_taken(point, taken):
            return point
    return last


def public_loiter(game: EmpireGame | None) -> dict | None:
    """The patch the feed carries. None until the player sets one."""
    if game is None:
        return None
    stored = stored_loiter(game)
    if stored is None:
        return None
    firm, centre, radius = stored
    return {"firm": firm, "lat": centre[0], "lng": centre[1], "radius_km": radius}


def strategic_spot(
    username: str,
    sign: str,
    now: float,
    taken: tuple[tuple[float, float], ...] = (),
) -> tuple[float, float]:
    """One taxi's own waiting spot. Another taxi's spot is left alone.

    The first draw is seeded as wait-{sign}. A taken spot uses the next seeded draw,
    so a test can replay which place this taxi got.
    """
    blocked = {(round(point[0], 5), round(point[1], 5)) for point in taken}
    pickup = DEPOT
    for attempt in range(16):
        salt = f"wait-{sign}" if attempt == 0 else f"wait-{sign}-{attempt}"
        _kind, pickup, _drop = draw_journey(username, now, salt)
        if not inside_circle(pickup):
            continue
        if (round(pickup[0], 5), round(pickup[1], 5)) not in blocked:
            return pickup
    return pickup


def nearby_empty_spot(
    origin: tuple[float, float],
    username: str,
    sign: str,
    now: float,
    taken: tuple[tuple[float, float], ...] = (),
) -> tuple[float, float] | None:
    """A published place within two miles. None when nothing free is that close.

    The seed includes the spell, so a test replays the place and the live tick can vary.
    """
    blocked = {(round(point[0], 5), round(point[1], 5)) for point in taken}
    near: list[tuple[float, float]] = []
    for _name, lat, lng in PLACES:
        point = (float(lat), float(lng))
        if haversine_km(origin, point) > EMPTY_MOVE_KM or haversine_km(origin, point) <= 0.05:
            continue
        if not inside_circle(point):
            continue
        if (round(point[0], 5), round(point[1], 5)) in blocked:
            continue
        near.append(point)
    if not near:
        return None
    rng = random.Random(_engine_seed(username, f"{_spell_key(now)}|near-{sign}"))
    return rng.choice(near)


def _account_of(username: str) -> str:
    head, sep, _tail = (username or "").partition("|")
    return head if sep else (username or "")


def firm_key(username: str, firm: str) -> str:
    """Draw seed and row key for one other firm. The player's own seed stays the bare username."""
    return f"{normalise_username(username)}|{firm}"


def _people_key(username: str) -> str:
    """Shared houses read one passenger queue. Every other account keeps its own."""
    account = _account_of(username)
    if account in SHARED_HOUSES:
        return WORLD
    return account


def _sign_index(sign: str) -> int:
    tail = (sign or "")[1:]
    return int(tail) if tail.isdigit() else 0


def _opening_sign(username: str, number: int) -> str:
    house = SHARED_HOUSES.get(username)
    prefix = house["prefix"] if house else "T"
    return f"{prefix}{number}"


def house_depot(username: str) -> tuple[float, float]:
    """Michael stays at Shiremoor. Noah and Reeve each keep one seeded depot."""
    house = SHARED_HOUSES.get(username)
    if house is None or house["depot"] == "shiremoor":
        return DEPOT
    return firm_depots(WORLD)[house["depot"]]


def _add_opening_cars(session: Session, username: str, now: float) -> None:
    depot = house_depot(username) if username in SHARED_HOUSES else DEPOT
    at_shiremoor = depot == DEPOT
    for sign, colour, offset in CARS:
        number = int(sign[1:])
        session.add(
            EmpireCar(
                username=username,
                sign=_opening_sign(username, number),
                colour=colour,
                status="at_depot",
                passenger="",
                lat=depot[0],
                lng=depot[1],
                depot_lat=0.0 if at_shiremoor else depot[0],
                depot_lng=0.0 if at_shiremoor else depot[1],
                route_json="[]",
                duration_s=0,
                leg_started=now,
                osrm=False,
                trail_json="[]",
                fare_name="",
                job="waiting",
                hold_until=now + offset,
                next_check=now + CHECK_S,
                depart_offset=offset,
            )
        )


def _ensure_world(session: Session, now: float) -> bool:
    """One passenger queue for the three houses. This row has no taxis."""
    world = session.get(EmpireGame, WORLD)
    if world is not None:
        return False
    session.add(
        EmpireGame(
            username=WORLD,
            created_at=str(int(now)),
            name_index=0,
            cash_pence=0,
            fare_in=0,
            miles_driven=0,
            firm_bookings="[]",
            trip_bookings="[]",
            company_name="",
        )
    )
    session.flush()
    return True


def _remember_company(game: EmpireGame) -> bool:
    """Fill a blank company name. A rename already stored on the row stays."""
    house = SHARED_HOUSES.get(game.username)
    if house is None or (game.company_name or "").strip():
        return False
    game.company_name = house["company"]
    return True


def firm_depot(
    username: str,
    firm: str,
    taken: tuple[tuple[float, float], ...] = (),
) -> tuple[float, float]:
    """One seeded depot for this firm. Not a town list, and not the player's depot."""
    blocked = {(round(point[0], 5), round(point[1], 5)) for point in taken}
    blocked.add((round(DEPOT[0], 5), round(DEPOT[1], 5)))
    blocked.add((round(place_point("Shiremoor")[0], 5), round(place_point("Shiremoor")[1], 5)))
    pool = [
        (lat, lng)
        for _name, lat, lng in PLACES
        if (round(lat, 5), round(lng, 5)) not in blocked
    ]
    rng = random.Random(_engine_seed(username, f"{firm}|depot"))
    return rng.choice(pool)


def firm_depots(username: str) -> dict[str, tuple[float, float]]:
    username = normalise_username(username)
    chosen: dict[str, tuple[float, float]] = {}
    taken: list[tuple[float, float]] = []
    for firm in FIRM_NAMES:
        depot = firm_depot(username, firm, tuple(taken))
        chosen[firm] = depot
        taken.append(depot)
    return chosen


def ensure_firms(username: str, now: float, session: Session) -> bool:
    """Add the other firms when this account does not have them yet. Do not add a player car."""
    username = normalise_username(username)
    added = False
    depots = firm_depots(username)
    for firm in FIRM_NAMES:
        key = firm_key(username, firm)
        if _fleet_cars(session, key):
            continue
        depot = depots[firm]
        prefix = FIRM_PREFIX[firm]
        for sign, colour, offset in CARS:
            number = int(sign[1:])
            session.add(
                EmpireCar(
                    username=key,
                    sign=f"{prefix}{number}",
                    colour=colour,
                    firm=firm,
                    depot_lat=depot[0],
                    depot_lng=depot[1],
                    status="at_depot",
                    job="waiting",
                    passenger="",
                    lat=depot[0],
                    lng=depot[1],
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
        added = True
    if added:
        session.flush()
    return added


def car_depot(car: EmpireCar) -> tuple[float, float]:
    """Opening cars use Shiremoor. Another firm's cars use the depot stored on the car."""
    lat = float(car.depot_lat or 0)
    lng = float(car.depot_lng or 0)
    if lat == 0.0 and lng == 0.0:
        return DEPOT
    return (lat, lng)


def car_position(car: EmpireCar, depot: tuple[float, float] | None = None) -> tuple[float, float]:
    """Where this car is now. A leg starts here."""
    home = car_depot(car) if depot is None else depot
    route = _loads(car.route_json, [])
    if route and car.status == "at_depot":
        end = (float(route[-1][0]), float(route[-1][1]))
        if haversine_km(end, home) > 0.05:
            return end
    if car.lat or car.lng:
        return (float(car.lat), float(car.lng))
    return home


def depot_takes_pickup(
    pickup: tuple[float, float],
    depots: tuple[tuple[float, float], ...],
    username: str,
    salt: str,
) -> tuple[float, float]:
    """A nearer depot is more likely to take the pickup. Distance is the territory."""
    rng = random.Random(_engine_seed(username, f"{salt}|depot"))
    weights = [1.0 / max(haversine_km(depot, pickup), 0.05) for depot in depots]
    return rng.choices(list(depots), weights=weights, k=1)[0]


def car_takes_a_break(username: str, sign: str, now: float, waiting: int, free: int) -> bool:
    """Likely when few people are waiting beside the free taxis, unlikely when they outnumber them.

    The same seed replays the choice. A later tick can draw again.
    """
    bucket = int(now) // BREAK_S
    rng = random.Random(_engine_seed(username, f"{_spell_key(now)}|{sign}|{bucket}|break"))
    if waiting > free:
        return rng.random() < 0.12
    return rng.random() < 0.88


def _should_break(car: EmpireCar, now: float) -> bool:
    session = object_session(car)
    if session is None:
        return False
    people = _people(session, _people_key(car.username))
    if any(person.sign == car.sign for person in people):
        return False
    free = sum(
        1 for other in _fleet_cars(session, car.username) if other.status == "at_depot" and car_may_depart(other)
    )
    return car_takes_a_break(car.username, car.sign, now, len(people), free)


def _sit_on_break(car: EmpireCar, now: float, depot: tuple[float, float]) -> None:
    """Sit at this car's own depot for a few minutes. Status stays at_depot."""
    car.passenger = ""
    car.fare_name = ""
    car.fare_lat = 0.0
    car.fare_lng = 0.0
    car.drop_lat = 0.0
    car.drop_lng = 0.0
    car.status = "at_depot"
    car.lat, car.lng = depot
    car.route_json = json.dumps([[depot[0], depot[1]], [depot[0], depot[1]]])
    car.duration_s = 0.0
    car.leg_started = now
    car.hold_until = now + BREAK_S
    car.osrm = True
    car.job = "on break"


def _people(session: Session, username: str) -> list[EmpirePerson]:
    return list(
        session.scalars(
            select(EmpirePerson).where(EmpirePerson.username == username).order_by(EmpirePerson.id)
        ).all()
    )


def _ensure_waiting_person(game: EmpireGame, session: Session, now: float) -> None:
    """Draw the people for this spell, or nobody when it is quiet.

    A busy spell may draw more people than there are free taxis. That does not add a car.
    """
    target = spell_target(game.username, now)
    for _ in range(20):
        people = _people(session, game.username)
        if len(people) >= target:
            return
        index = int(game.name_index or 0)
        kind, pickup, drop = draw_journey(game.username, now, f"person-{index}")
        if not inside_circle(pickup) or not inside_circle(drop) or pickup == drop:
            return
        session.add(
            EmpirePerson(
                username=game.username,
                name=NAMES[index % len(NAMES)],
                lat=pickup[0],
                lng=pickup[1],
                sign="",
                drop_lat=drop[0],
                drop_lng=drop[1],
                arrived=now,
                journey=kind,
            )
        )
        game.name_index = index + 1
        session.flush()


def _claim_closest(people: list[EmpirePerson], cars: list[EmpireCar], taken: set[str]) -> None:
    """One free car takes the closest person who still has no taxi.

    Equal distances keep the older person and the lower sign. A car on a leg is not here.
    """
    pending = [person for person in people if not person.sign]
    free = [
        car
        for car in cars
        if car.status == "at_depot" and car.sign not in taken and car_may_depart(car)
    ]
    while pending and free:
        best_rank = None
        best_person = None
        best_car = None
        for person in pending:
            pickup = (float(person.lat), float(person.lng))
            for car in free:
                rank = (
                    haversine_km(car_position(car), pickup),
                    int(person.id or 0),
                    int(car.sign[1:]),
                    car.sign,
                )
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    best_person = person
                    best_car = car
        best_person.sign = best_car.sign
        taken.add(best_car.sign)
        pending.remove(best_person)
        free.remove(best_car)


def _allocate_one_free_taxi(game: EmpireGame, session: Session) -> None:
    """Each waiting person is claimed by the closest free taxi. A car on a leg is left alone.

    People left over do not add a car. Two people do not claim the same taxi.
    The app session does not autoflush, so this reads the cars already in memory.
    """
    people = _people(session, game.username)
    taken = {person.sign for person in people if person.sign}
    _claim_closest(people, _cars(session, game.username), taken)


def _allocate_firm_leftovers(game: EmpireGame, session: Session) -> None:
    """Other firms take people the player's cars are not covering, by the closest car.

    The player's cars have already claimed. This does not add a car.
    """
    people = _people(session, game.username)
    taken = {person.sign for person in people if person.sign}
    firm_cars: list[EmpireCar] = []
    for firm in FIRM_NAMES:
        firm_cars.extend(_fleet_cars(session, firm_key(game.username, firm)))
    _claim_closest(people, firm_cars, taken)
    session.flush()


def _waiting_away_from_depot(car: EmpireCar) -> bool:
    """True when an idle car is already at a place that is not its depot."""
    route = _loads(car.route_json, [])
    if not route:
        return False
    end = (float(route[-1][0]), float(route[-1][1]))
    return haversine_km(end, car_depot(car)) > 0.05


def _start_fare(car: EmpireCar, game: EmpireGame, now: float) -> None:
    if airport_sign(car.sign):
        pickup, drop = airport_ends(car.username)
    elif is_morning(now):
        pickup, drop = morning_ends(car.username)
    elif is_day(now):
        pickup, drop = day_ends(car.username)
    elif is_evening(now):
        pickup, drop = evening_ends(car.username)
    elif is_night(now):
        pickup, drop = night_ends(car.username)
    else:
        pickup, drop = journey_ends(car.username)
    name = _next_name(game)
    car.passenger = name
    car.fare_name = name
    car.fare_lat, car.fare_lng = pickup
    car.drop_lat, car.drop_lng = drop
    _begin(car, game, car_position(car), pickup, now, "to_passenger")


def _car_miles(car: EmpireCar) -> float:
    return float(getattr(car, "miles_driven", 0) or 0)


def _car_fare(car: EmpireCar) -> int:
    return int(getattr(car, "fare_in", 0) or 0)


def _add_car_miles(car: EmpireCar, miles: float) -> None:
    if miles <= 0:
        return
    car.miles_driven = _car_miles(car) + float(miles)


def _add_car_fare(car: EmpireCar, pence: int) -> None:
    if pence <= 0:
        return
    car.fare_in = _car_fare(car) + int(pence)


def _remember_miles(car: EmpireCar) -> None:
    """Keep the miles on a leg that just finished. Another firm's miles stay off this account."""
    route = _loads(car.route_json, [])
    if len(route) < 2:
        return
    miles = route_miles(route)
    _add_car_miles(car, miles)
    if car.firm:
        return
    session = object_session(car)
    if session is None:
        return
    game = session.get(EmpireGame, _account_of(car.username))
    if game is None:
        return
    game.miles_driven = float(game.miles_driven or 0) + miles


def _finish_empty_move(car: EmpireCar, now: float) -> None:
    """The car has reached the better spot. That arrival is not a fare."""
    _remember_miles(car)
    route = _loads(car.route_json, [])
    end = (float(route[-1][0]), float(route[-1][1])) if route else car_depot(car)
    car.lat, car.lng = end
    car.passenger = ""
    car.fare_name = ""
    car.fare_lat = 0.0
    car.fare_lng = 0.0
    car.drop_lat = 0.0
    car.drop_lng = 0.0
    car.status = "at_depot"
    car.route_json = json.dumps([[end[0], end[1]], [end[0], end[1]]])
    car.duration_s = 0.0
    car.leg_started = now
    car.osrm = True
    car.hold_until = now + DEPOT_PAUSE_S


def current_job(car: EmpireCar) -> str:
    """One job. The four movement statuses stay the words they already are."""
    if car.status == "to_better_spot":
        return "travelling to a better spot"
    if car.status == "to_passenger":
        return "collecting"
    if car.status == "with_passenger":
        return "carrying"
    if car.status == "returning":
        return "returning"
    if car.status == "at_depot" and float(car.hold_until) - float(car.leg_started) >= BREAK_S - 1:
        return "on break"
    return "waiting"


def _store_job(car: EmpireCar) -> None:
    car.job = current_job(car)


def _allocated_person(car: EmpireCar) -> EmpirePerson | None:
    session = object_session(car)
    if session is None:
        return None
    return session.scalar(
        select(EmpirePerson).where(
            EmpirePerson.username == _people_key(car.username),
            EmpirePerson.sign == car.sign,
        )
    )


def _start_arrived_trip(car: EmpireCar, game: EmpireGame, now: float) -> None:
    """The allocated taxi has arrived. Run the journey drawn for that person.

    800000 is not a fare. fare_in is unchanged.
    """
    person = _allocated_person(car)
    if person is not None and (person.drop_lat or person.drop_lng):
        pickup = (float(person.lat), float(person.lng))
        drop = (float(person.drop_lat), float(person.drop_lng))
    else:
        pickup, drop = hour_ends(car.username, now)
    if person is not None and person.name:
        name = person.name
    else:
        name = _next_name(game)
    route = _loads(car.route_json, [])
    start = (float(route[-1][0]), float(route[-1][1])) if route else pickup
    car.passenger = name
    car.fare_name = ""
    car.fare_lat, car.fare_lng = pickup
    car.drop_lat, car.drop_lng = drop
    car.journey = (person.journey or "") if person is not None else ""
    _begin(car, game, start, drop, now, "with_passenger")
    _collected_person_leaves(car)


def _collected_person_leaves(car: EmpireCar) -> None:
    """The claimed person leaves once this taxi begins carrying them.

    A person who is still waiting stays. The car keeps the journey it already copied.
    """
    session = object_session(car)
    if session is None:
        return
    person = session.scalar(
        select(EmpirePerson).where(
            EmpirePerson.username == _people_key(car.username),
            EmpirePerson.sign == car.sign,
        )
    )
    if person is None:
        return
    session.delete(person)
    session.flush()


def _trip_rows(game: EmpireGame) -> list[dict]:
    raw = "[]" if game is None else (game.trip_bookings or "[]")
    rows = _loads(raw, [])
    return [row for row in rows if isinstance(row, dict)]


def _remember_trip_booking(
    game: EmpireGame,
    car: EmpireCar,
    pickup: tuple[float, float],
    drop: tuple[float, float],
    pence: int,
    completed: float,
) -> None:
    """Append one finished trip for this account. Keep the last 12. Do not touch fare_in."""
    if (car.firm or "").strip():
        return
    pence = int(pence)
    if pence == NEW_CAR_PENCE:
        return
    rows = _trip_rows(game)
    rows.append(
        {
            "sign": car.sign,
            "pickup": place_name(pickup),
            "drop": place_name(drop),
            "pence": pence,
            "completed": int(completed),
        }
    )
    game.trip_bookings = json.dumps(rows[-FIRM_BOOKINGS_KEPT:])


def _public_trip_bookings(game: EmpireGame) -> list[dict]:
    out = []
    for row in _trip_rows(game):
        pence = int(row.get("pence") or 0)
        if pence == NEW_CAR_PENCE:
            continue
        out.append(
            {
                "sign": str(row.get("sign") or ""),
                "pickup": str(row.get("pickup") or ""),
                "drop": str(row.get("drop") or ""),
                "pence": pence,
                "completed": int(row.get("completed") or 0),
            }
        )
    return out[-FIRM_BOOKINGS_KEPT:]


def _booking_rows(game: EmpireGame) -> list[dict]:
    raw = "[]" if game is None else (game.firm_bookings or "[]")
    rows = _loads(raw, [])
    return [row for row in rows if isinstance(row, dict)]


def _remember_firm_booking(
    game: EmpireGame,
    car: EmpireCar,
    pickup: tuple[float, float],
    drop: tuple[float, float],
    pence: int,
    completed: float,
) -> None:
    """Append one finished trip for this firm only. Keep the last 12. Do not touch fare_in."""
    firm = (car.firm or "").strip()
    if firm not in FIRM_NAMES:
        return
    pence = int(pence)
    if pence == NEW_CAR_PENCE:
        return
    rows = _booking_rows(game)
    rows.append(
        {
            "firm": firm,
            "sign": car.sign,
            "pickup": place_name(pickup),
            "drop": place_name(drop),
            "pence": pence,
            "completed": int(completed),
        }
    )
    kept: list[dict] = []
    seen = {name: 0 for name in FIRM_NAMES}
    for row in reversed(rows):
        name = row.get("firm")
        if name not in seen:
            continue
        seen[name] += 1
        if seen[name] <= FIRM_BOOKINGS_KEPT:
            kept.append(row)
    kept.reverse()
    game.firm_bookings = json.dumps(kept)


def _public_bookings(game: EmpireGame, firm: str) -> list[dict]:
    out = []
    for row in _booking_rows(game):
        if row.get("firm") != firm:
            continue
        out.append(
            {
                "sign": str(row.get("sign") or ""),
                "pickup": str(row.get("pickup") or ""),
                "drop": str(row.get("drop") or ""),
                "pence": int(row.get("pence") or 0),
                "completed": int(row.get("completed") or 0),
            }
        )
    return out[-FIRM_BOOKINGS_KEPT:]


def _finish_leg(car: EmpireCar, game: EmpireGame, now: float) -> None:
    if car.status == "to_better_spot" and _allocated_person(car) is not None:
        _start_arrived_trip(car, game, now)
        return
    if car.status == "to_better_spot" or (car.status == "to_passenger" and not car.passenger and not car.fare_name):
        _finish_empty_move(car, now)
        return
    if car.status == "to_passenger":
        _remember_miles(car)
        start = (car.fare_lat, car.fare_lng)
        end = (car.drop_lat, car.drop_lng)
        car.fare_name = ""
        _begin(car, game, start, end, now, "with_passenger")
        _collected_person_leaves(car)
        return
    if car.status == "with_passenger":
        tariff = completed_trip_tariff(car)
        route = _loads(car.route_json, [])
        if len(route) >= 2:
            pickup = (float(route[0][0]), float(route[0][1]))
            drop = (float(route[-1][0]), float(route[-1][1]))
        else:
            pickup = (float(car.fare_lat), float(car.fare_lng))
            drop = (float(car.drop_lat), float(car.drop_lng))
        _add_car_fare(car, tariff)
        if not (car.firm or ""):
            game.fare_in = int(game.fare_in or 0) + tariff
            _remember_trip_booking(game, car, pickup, drop, tariff, now)
        else:
            _remember_firm_booking(game, car, pickup, drop, tariff, now)
        _remember_miles(car)
        if route:
            start = (float(route[-1][0]), float(route[-1][1]))
        else:
            start = (float(car.drop_lat), float(car.drop_lng))
        car.passenger = ""
        car.fare_name = ""
        car.fare_lat = 0.0
        car.fare_lng = 0.0
        car.drop_lat = 0.0
        car.drop_lng = 0.0
        car.status = "at_depot"
        car.lat, car.lng = start
        car.route_json = json.dumps([[start[0], start[1]], [start[0], start[1]]])
        car.duration_s = 0
        car.leg_started = now
        car.hold_until = now
        car.osrm = True
        car.job = "waiting"
        return
    if car.status == "returning":
        _remember_miles(car)
        route = _loads(car.route_json, [])
        home = (float(route[-1][0]), float(route[-1][1])) if route else car_depot(car)
        car.status = "at_depot"
        car.passenger = ""
        car.fare_name = ""
        car.lat, car.lng = home
        car.route_json = json.dumps([[home[0], home[1]], [home[0], home[1]]])
        car.duration_s = 0
        car.leg_started = now
        car.osrm = True
        if _should_break(car, now):
            car.hold_until = now + BREAK_S
            car.job = "on break"
        else:
            car.hold_until = now + DEPOT_PAUSE_S


def _check_in(car: EmpireCar, now: float) -> None:
    trail = _loads(car.trail_json, [])
    while now + 1e-6 >= car.next_check:
        trail.append([round(float(car.lng), 6), round(float(car.lat), 6), int(car.next_check)])
        car.next_check += CHECK_S
    cutoff = int(now) - TRAIL_S
    trail = [point for point in trail if isinstance(point, list) and len(point) >= 3 and int(point[2]) >= cutoff]
    car.trail_json = json.dumps(trail)


def _stop_far_empty_leg(car: EmpireCar, now: float) -> None:
    """A passengerless empty leg whose end is still more than two miles away stops here."""
    here = (float(car.lat), float(car.lng))
    car.passenger = ""
    car.fare_name = ""
    car.fare_lat = 0.0
    car.fare_lng = 0.0
    car.drop_lat = 0.0
    car.drop_lng = 0.0
    car.status = "at_depot"
    car.lat, car.lng = here
    car.route_json = json.dumps([[here[0], here[1]], [here[0], here[1]]])
    car.duration_s = 0.0
    car.leg_started = now
    car.osrm = True
    car.hold_until = now


def advance_car(car: EmpireCar, game: EmpireGame, now: float) -> None:
    if _fault is not None and _fault(car):
        raise RuntimeError("empire car fault")
    if car.status == "to_better_spot" and not car.passenger and not car.fare_name:
        route = _loads(car.route_json, [])
        if len(route) >= 2:
            here = (float(car.lat), float(car.lng))
            end = (float(route[-1][0]), float(route[-1][1]))
            if haversine_km(here, end) > EMPTY_MOVE_KM:
                _stop_far_empty_leg(car, now)
                _store_job(car)
                _check_in(car, now)
                return
    for _ in range(48):
        if car.status == "at_depot":
            home = car_depot(car)
            if _waiting_away_from_depot(car):
                route = _loads(car.route_json, [])
                car.lat, car.lng = float(route[-1][0]), float(route[-1][1])
            else:
                car.lat, car.lng = home
            if now < car.hold_until or not car_may_depart(car):
                break
            break
        elapsed = now - car.leg_started
        route = _loads(car.route_json, [])
        if car.duration_s <= 0 or elapsed < car.duration_s:
            fraction = 0.0 if car.duration_s <= 0 else elapsed / car.duration_s
            if route:
                car.lat, car.lng = point_at(route, fraction)
            break
        _finish_leg(car, game, car.leg_started + car.duration_s)
    _store_job(car)
    _check_in(car, now)


def _collect_claimed(car: EmpireCar, game: EmpireGame, person: EmpirePerson, now: float) -> None:
    """The claimed taxi collects that person, from where the car already is, then their drop."""
    start = car_position(car, car_depot(car))
    pickup = (float(person.lat), float(person.lng))
    drop = (float(person.drop_lat), float(person.drop_lng))
    name = person.name or _next_name(game)
    car.passenger = name
    car.fare_name = name
    car.fare_lat, car.fare_lng = pickup
    car.drop_lat, car.drop_lng = drop
    _begin(car, game, start, pickup, now, "to_passenger")
    _store_job(car)


def _purchase_pause_just_ended(car: EmpireCar) -> bool:
    """True only for the bought car's four-second pause, before its first route.

    Opening cars and other firms do not match. A later pause has already run a route.
    """
    if car.sign in OPENING_SIGNS or (car.firm or "") or 1 <= _sign_index(car.sign) <= 10:
        return False
    if car.osrm or car.passenger or car.fare_name:
        return False
    span = float(car.hold_until) - float(car.leg_started)
    return abs(span - float(DEPOT_PAUSE_S)) < 0.001


def _take_first_bought_fare(
    car: EmpireCar,
    game: EmpireGame,
    session: Session,
    now: float,
    taken: list[tuple[float, float]],
) -> bool:
    """After the purchase pause, this car takes one fare inside the circle.

    This does not add a car and does not change the fare total. A later pause does not do it again.
    """
    if not _purchase_pause_just_ended(car):
        return False
    account = game.username
    for attempt in range(16):
        kind, pickup, drop = draw_journey(account, now, f"bought-{car.sign}-{attempt}")
        if not inside_circle(pickup) or not inside_circle(drop):
            continue
        if haversine_km(pickup, drop) <= 0.05:
            continue
        person = EmpirePerson(
            username=_people_key(account),
            name=_next_name(game),
            lat=pickup[0],
            lng=pickup[1],
            sign=car.sign,
            drop_lat=drop[0],
            drop_lng=drop[1],
            arrived=now,
            journey=kind,
        )
        session.add(person)
        session.flush()
        _collect_claimed(car, game, person, now)
        taken.append(pickup)
        return True
    return False


def _travel_to_a_better_spot(
    game: EmpireGame,
    session: Session,
    now: float,
    draw_as: str | None = None,
) -> None:
    """Claimed taxis collect their own person. Everyone else hunts or takes a break.

    Each idle taxi draws its own spot, so the cars do not stack and do not share one path.
    A car that is collecting, carrying, or returning keeps that leg.
    The depot is for a break. 800000 is not a fare.
    Another firm passes its own draw seed. The player's seed stays the bare username.
    A bought car takes one fare inside the circle when its four-second purchase pause ends.
    """
    fleet_name = draw_as or game.username
    patch = loiter_for_fleet(game, draw_as)
    people = _people(session, _people_key(game.username))
    claimed = {person.sign: person for person in people if person.sign}
    cars = _fleet_cars(session, fleet_name) if draw_as else _cars(session, game.username)
    free = sum(1 for car in cars if car.status == "at_depot" and car_may_depart(car))
    taken: list[tuple[float, float]] = [(float(person.lat), float(person.lng)) for person in people]
    for car in cars:
        if car.status == "at_depot":
            continue
        route = _loads(car.route_json, [])
        if len(route) >= 2:
            taken.append((float(route[-1][0]), float(route[-1][1])))
    for car in cars:
        try:
            if _fault is not None and _fault(car):
                raise RuntimeError("empire car fault")
            if car.status != "at_depot" or not car_may_depart(car):
                continue
            if now < float(car.hold_until):
                continue
            person = claimed.get(car.sign)
            if person is not None:
                _collect_claimed(car, game, person, now)
                continue
            if _take_first_bought_fare(car, game, session, now, taken):
                continue
            depot = car_depot(car)
            start = car_position(car, depot)
            if patch is not None:
                centre, radius = patch
                if _inside_loiter(start, centre, radius) and not _point_taken(start, tuple(taken)):
                    if float(car.hold_until) - float(car.leg_started) >= BREAK_S - 1:
                        car.leg_started = now
                        car.hold_until = now
                        _store_job(car)
                    taken.append(start)
                    continue
                spot = loiter_spot(fleet_name, car.sign, now, centre, radius, tuple(taken))
                if not _inside_loiter(spot, centre, radius) or _point_taken(spot, tuple(taken)):
                    continue
                if _inside_loiter(start, centre, radius) and haversine_km(start, spot) <= 0.05:
                    taken.append(start)
                    continue
                taken.append(spot)
                car.passenger = ""
                car.fare_name = ""
                car.fare_lat = 0.0
                car.fare_lng = 0.0
                car.drop_lat = 0.0
                car.drop_lng = 0.0
                _begin(car, game, start, spot, now, "to_better_spot")
                _store_job(car)
                continue
            if car_takes_a_break(fleet_name, car.sign, now, len(people), free):
                if haversine_km(start, depot) > 0.05:
                    car.passenger = ""
                    car.fare_name = ""
                    car.fare_lat = 0.0
                    car.fare_lng = 0.0
                    car.drop_lat = 0.0
                    car.drop_lng = 0.0
                    _begin(car, game, start, depot, now, "returning")
                else:
                    _sit_on_break(car, now, depot)
                _store_job(car)
                continue
            spot = nearby_empty_spot(start, fleet_name, car.sign, now, tuple(taken))
            if spot is None or not inside_circle(spot) or haversine_km(start, spot) > EMPTY_MOVE_KM:
                continue
            if haversine_km(start, spot) <= 0.05:
                continue
            taken.append(spot)
            car.passenger = ""
            car.fare_name = ""
            car.fare_lat = 0.0
            car.fare_lng = 0.0
            car.drop_lat = 0.0
            car.drop_lng = 0.0
            _begin(car, game, start, spot, now, "to_better_spot")
            _store_job(car)
        except Exception:
            log.exception("empire car %s %s failed", car.username, car.sign)


def tick_all(now: float | None = None, session: Session | None = None) -> None:
    if now is None:
        now = time.time()
    own_session = session is None
    if session is None:
        session = SessionLocal()
    try:
        games = list(session.scalars(select(EmpireGame)).all())
        shared = [game for game in games if game.username in SHARED_HOUSES]
        if shared:
            _ensure_world(session, now)
            world = session.get(EmpireGame, WORLD)
            _ensure_waiting_person(world, session, now)
        for game in games:
            if game.username == WORLD or game.username in SHARED_HOUSES:
                continue
            ensure_firms(game.username, now, session)
            _ensure_waiting_person(game, session, now)
            for car in _cars(session, game.username):
                try:
                    advance_car(car, game, now)
                except Exception:
                    log.exception("empire car %s %s failed", car.username, car.sign)
            _ensure_waiting_person(game, session, now)
            _allocate_one_free_taxi(game, session)
            _travel_to_a_better_spot(game, session, now)
            for firm in FIRM_NAMES:
                for car in _fleet_cars(session, firm_key(game.username, firm)):
                    try:
                        advance_car(car, game, now)
                    except Exception:
                        log.exception("empire car %s %s failed", car.username, car.sign)
            _ensure_waiting_person(game, session, now)
            _allocate_firm_leftovers(game, session)
            for firm in FIRM_NAMES:
                _travel_to_a_better_spot(game, session, now, draw_as=firm_key(game.username, firm))
        if shared:
            for game in shared:
                for car in _cars(session, game.username):
                    try:
                        advance_car(car, game, now)
                    except Exception:
                        log.exception("empire car %s %s failed", car.username, car.sign)
            world = session.get(EmpireGame, WORLD)
            _ensure_waiting_person(world, session, now)
            people = _people(session, WORLD)
            taken = {person.sign for person in people if person.sign}
            rivals: list[EmpireCar] = []
            for game in shared:
                rivals.extend(_cars(session, game.username))
            _claim_closest(people, rivals, taken)
            for game in shared:
                _travel_to_a_better_spot(game, session, now)
        session.commit()
    finally:
        if own_session:
            session.close()


def driver_share_pence(fare_in: int) -> int:
    """Driver share is half the fare, in integer pence."""
    return int(fare_in) // 2


def fuel_pence(miles: float) -> int:
    """Fuel is twenty pence a mile, including the empty return."""
    return int(round(float(miles) * FUEL_PENCE_PER_MILE))


def new_car_may_leave(paid_pence: int) -> bool:
    """A new car stays at the depot until 800000 pence is paid."""
    return int(paid_pence) >= NEW_CAR_PENCE


def car_may_depart(car: EmpireCar) -> bool:
    """The opening ten may leave. A bought car may leave only after it is paid for."""
    if car.sign in OPENING_SIGNS or 1 <= _sign_index(car.sign) <= 10:
        return True
    return car.depart_offset >= 0


def pay_new_car(car: EmpireCar, pence: int) -> bool:
    """Record the 800000 pence line. A smaller payment does not let the car leave."""
    if not new_car_may_leave(pence):
        return False
    if car.depart_offset < 0:
        car.depart_offset = 0
    return True


def buy_car(username: str, pence: int, now: float | None = None, session: Session | None = None) -> str | None:
    """Spend 800000 pence to add T11 when this account's money covers it. A smaller payment adds nothing."""
    if not new_car_may_leave(pence):
        return None
    username = normalise_username(username)
    own_session = session is None
    if session is None:
        session = SessionLocal()
    if now is None:
        now = time.time()
    try:
        game = ensure_game(username, now, session)
        bought = _opening_sign(username, 11)
        cars = _cars(session, username)
        existing = next((car for car in cars if car.sign == bought), None)
        if existing is not None and existing.depart_offset >= 0:
            return bought
        if account_balance(game, cars, now) < NEW_CAR_PENCE:
            return None
        if existing is not None:
            existing.depart_offset = 0
            existing.leg_started = now
            existing.hold_until = now + DEPOT_PAUSE_S
            game.cash_pence = int(game.cash_pence or 0) - NEW_CAR_PENCE
            session.commit()
            return bought
        game.cash_pence = int(game.cash_pence or 0) - NEW_CAR_PENCE
        home = house_depot(username)
        at_shiremoor = home == DEPOT
        session.add(
            EmpireCar(
                username=username,
                sign=bought,
                colour="#2dd4bf",
                status="at_depot",
                job="waiting",
                passenger="",
                lat=home[0],
                lng=home[1],
                depot_lat=0.0 if at_shiremoor else home[0],
                depot_lng=0.0 if at_shiremoor else home[1],
                route_json="[]",
                duration_s=0,
                leg_started=now,
                osrm=False,
                trail_json="[]",
                fare_name="",
                hold_until=now + DEPOT_PAUSE_S,
                next_check=now + CHECK_S,
                depart_offset=0,
            )
        )
        session.commit()
        return bought
    finally:
        if own_session:
            session.close()


def _job_has_left(car: EmpireCar, now: float) -> bool:
    """The car is off the start of this leg. Standing at the depot has not left."""
    if car.status == "at_depot":
        return False
    route = _loads(car.route_json, [])
    if len(route) < 2 or float(car.duration_s) <= 0:
        return False
    start = (float(route[0][0]), float(route[0][1]))
    if haversine_km((float(car.lat), float(car.lng)), start) > 0.05:
        return True
    elapsed = max(0.0, float(now) - float(car.leg_started))
    fraction = min(1.0, elapsed / float(car.duration_s))
    return haversine_km(point_at(route, fraction), start) > 0.05


def _stop_passenger_car(car: EmpireCar, now: float) -> None:
    """Leave the car where it is. The passenger trip is gone, so the feed does not price it again."""
    point = (float(car.lat), float(car.lng))
    car.passenger = ""
    car.fare_name = ""
    car.fare_lat = 0.0
    car.fare_lng = 0.0
    car.drop_lat = 0.0
    car.drop_lng = 0.0
    car.status = "at_depot"
    car.lat, car.lng = point
    car.route_json = json.dumps([[point[0], point[1]], [point[0], point[1]]])
    car.duration_s = 0
    car.leg_started = now
    car.hold_until = now
    car.osrm = True
    car.job = "waiting"


def cancel_job(username: str, sign: str, now: float | None = None, session: Session | None = None) -> bool:
    """Cancel a passenger job.

    Before the car moves, fare and fuel stay as they are.
    After it has left, the miles already driven on this leg become fuel and there is no fare.
    """
    username = normalise_username(username)
    sign = (sign or "").strip()
    if not sign:
        return False
    own_session = session is None
    if session is None:
        session = SessionLocal()
    if now is None:
        now = time.time()
    try:
        game = ensure_game(username, now, session)
        car = next((item for item in _cars(session, username) if item.sign == sign), None)
        if car is None:
            return False
        person = _allocated_person(car)
        collecting = car.status == "to_passenger" and bool(car.passenger or car.fare_name)
        carrying = car.status == "with_passenger"
        waiting_here = car.status == "at_depot" and person is not None
        if _job_has_left(car, now):
            if not collecting and not carrying:
                return False
            driven = driven_miles(car, now)
            _add_car_miles(car, driven)
            game.miles_driven = float(game.miles_driven or 0) + driven
        elif not collecting and not waiting_here:
            return False
        if person is not None:
            person.sign = ""
        if collecting or carrying:
            _stop_passenger_car(car, now)
        session.commit()
        return True
    finally:
        if own_session:
            session.close()


def set_loiter(
    username: str,
    lat: float,
    lng: float,
    radius_km: float,
    firm: str = "",
    now: float | None = None,
    session: Session | None = None,
) -> bool:
    """Store the patch where that firm's idle taxis loiter. Cash and the cars stay."""
    username = normalise_username(username)
    firm_name = (firm or "").strip().lower()
    if firm_name not in ("", *FIRM_NAMES):
        return False
    try:
        centre = (float(lat), float(lng))
        radius = float(radius_km)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(centre[0]) or not math.isfinite(centre[1]) or not math.isfinite(radius):
        return False
    if radius < LOITER_MIN_KM or radius > LOITER_MAX_KM or not inside_circle(centre):
        return False
    own_session = session is None
    if session is None:
        session = SessionLocal()
    if now is None:
        now = time.time()
    try:
        game = ensure_game(username, now, session)
        game.loiter_firm = firm_name
        game.loiter_lat = centre[0]
        game.loiter_lng = centre[1]
        game.loiter_km = radius
        session.commit()
        return True
    finally:
        if own_session:
            session.close()


def route_miles(route: list) -> float:
    total = 0.0
    for index in range(len(route) - 1):
        start = (float(route[index][0]), float(route[index][1]))
        end = (float(route[index + 1][0]), float(route[index + 1][1]))
        total += haversine_km(start, end) / KM_PER_MILE
    return total


def driven_miles(car: EmpireCar, now: float) -> float:
    """Miles along the current leg. Standing does not use this figure."""
    if car.status == "at_depot":
        return 0.0
    route = _loads(car.route_json, [])
    if len(route) < 2 or car.duration_s <= 0:
        return 0.0
    elapsed = max(0.0, float(now) - float(car.leg_started))
    fraction = min(1.0, elapsed / float(car.duration_s))
    return route_miles(route) * fraction


# Tariff one, 11 November 2024. Lengths and times are tenths.
# The flag and the further unit are pence. Distance or waiting time finishes a unit, not both.
TARIFF_ONE_UNIT_YARDS = 1613
TARIFF_ONE_UNIT_SECONDS = 468
TARIFF_ONE_FLAG_PENCE = 240
TARIFF_ONE_FURTHER_PENCE = 25
YARDS_PER_MILE = 1760


def _tariff_units(yards: int, seconds: int, unit_yards: int, unit_seconds: int) -> int:
    units = 0
    into_yards = 0
    into_seconds = 0
    guard = 0
    while yards > 0 or seconds > 0:
        guard += 1
        if guard > 100000:
            break
        need_yards = unit_yards - into_yards
        need_seconds = unit_seconds - into_seconds
        if need_yards <= 0 or need_seconds <= 0:
            units += 1
            into_yards = 0
            into_seconds = 0
            continue
        if seconds == 0:
            if yards >= need_yards:
                yards -= need_yards
                units += 1
                into_yards = 0
                into_seconds = 0
            else:
                into_yards += yards
                yards = 0
            continue
        if yards == 0:
            if seconds >= need_seconds:
                seconds -= need_seconds
                units += 1
                into_yards = 0
                into_seconds = 0
            else:
                into_seconds += seconds
                seconds = 0
            continue
        distance_first = yards >= need_yards and seconds * need_yards <= need_seconds * yards
        time_first = seconds >= need_seconds and yards * need_seconds < need_yards * seconds
        if distance_first:
            yards_left = yards - need_yards
            seconds = seconds * yards_left // yards
            yards = yards_left
            units += 1
            into_yards = 0
            into_seconds = 0
        elif time_first:
            seconds_left = seconds - need_seconds
            yards = yards * seconds_left // seconds
            seconds = seconds_left
            units += 1
            into_yards = 0
            into_seconds = 0
        else:
            into_yards += yards
            into_seconds += seconds
            yards = 0
            seconds = 0
    if into_yards > 0 or into_seconds > 0:
        units += 1
    return units


def north_tyneside_tariff_pence(miles: float, seconds: float) -> int:
    """Tariff one for one hiring. One mile is 490. Three miles are 1040."""
    yards = int(round(float(miles) * YARDS_PER_MILE * 10))
    ticks = int(round(max(0.0, float(seconds)) * 10))
    if yards < 0:
        yards = 0
    units = _tariff_units(yards, ticks, TARIFF_ONE_UNIT_YARDS, TARIFF_ONE_UNIT_SECONDS)
    if units == 0:
        return 0
    return TARIFF_ONE_FLAG_PENCE + (units - 1) * TARIFF_ONE_FURTHER_PENCE


def completed_trip_tariff(car: EmpireCar) -> int:
    """Pence for the with_passenger route that just finished. 800000 is not a fare."""
    route = _loads(car.route_json, [])
    miles = route_miles(route) if len(route) >= 2 else 0.0
    return north_tyneside_tariff_pence(miles, float(car.duration_s))


def _passenger_ends(car: EmpireCar) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Pickup and drop of the passenger journey this car is on. None when it is not."""
    if car.status == "with_passenger":
        route = _loads(car.route_json, [])
        if len(route) >= 2:
            return (
                (float(route[0][0]), float(route[0][1])),
                (float(route[-1][0]), float(route[-1][1])),
            )
        return (float(car.fare_lat), float(car.fare_lng)), (float(car.drop_lat), float(car.drop_lng))
    if car.status == "to_passenger" and (car.passenger or car.fare_name):
        return (float(car.fare_lat), float(car.fare_lng)), (float(car.drop_lat), float(car.drop_lng))
    return None


def trip_fields(car: EmpireCar) -> dict[str, str | int]:
    """Place names and tariff pence for the journey this car is on.

    trip_pence is 0 when there is no passenger trip. 800000 is not a trip fare.
    """
    ends = _passenger_ends(car)
    if ends is None:
        return {"pickup": "", "drop": "", "trip_pence": 0}
    pickup, drop = ends
    if car.status == "with_passenger":
        pence = completed_trip_tariff(car)
    elif inside_circle(pickup) and inside_circle(drop):
        miles = haversine_km(pickup, drop) / KM_PER_MILE
        seconds = max(miles, 0.001) / MPH * 3600.0
        pence = north_tyneside_tariff_pence(miles, seconds)
    else:
        pence = 0
    if pence == NEW_CAR_PENCE:
        pence = 0
    return {"pickup": place_name(pickup), "drop": place_name(drop), "trip_pence": int(pence)}


def money_lines(car_count: int, fare_in: int = 0, miles: float = 0.0) -> dict[str, int]:
    """In-game money for one account. Integer pence. No company totals."""
    fare = int(fare_in)
    return {
        "fare_in": fare,
        "driver_share": driver_share_pence(fare),
        "fuel": fuel_pence(miles),
        "standing": STANDING_PENCE * int(car_count),
    }


def account_balance(game: EmpireGame | None, cars: list[EmpireCar], now: float) -> int:
    """Pence this account can spend. Fares come in. The driver's share, fuel, standing, and taxi purchases come out."""
    fare = 0 if game is None else int(game.fare_in or 0)
    done = 0.0 if game is None else float(game.miles_driven or 0)
    miles = done + sum(driven_miles(car, now) for car in cars)
    lines = money_lines(len(cars), fare_in=fare, miles=miles)
    cash = 0 if game is None else int(game.cash_pence or 0)
    return cash + lines["fare_in"] - lines["driver_share"] - lines["fuel"] - lines["standing"]


def car_is_for_sale(car: EmpireCar) -> bool:
    """A bought car that is not yet paid for can still be bought. The opening ten cannot."""
    return car.sign not in OPENING_SIGNS and float(car.depart_offset) < 0


def for_sale_rows(cars: list[EmpireCar]) -> list[dict]:
    """Taxis the account does not own yet. T11 waits at the depot until it is paid for."""
    owned = {car.sign for car in cars}
    rows: list[dict] = []
    if "T11" not in owned:
        rows.append(
            {
                "sign": "T11",
                "colour": "#2dd4bf",
                "lat": DEPOT[0],
                "lng": DEPOT[1],
                "pence": NEW_CAR_PENCE,
            }
        )
    for car in cars:
        if not car_is_for_sale(car):
            continue
        rows.append(
            {
                "sign": car.sign,
                "colour": car.colour,
                "lat": float(car.lat),
                "lng": float(car.lng),
                "pence": NEW_CAR_PENCE,
            }
        )
    return rows


def leg_seconds_left(car: EmpireCar, now: float) -> int:
    """Whole seconds left on this leg. A car at the depot has none."""
    if car.status == "at_depot" or float(car.duration_s) <= 0:
        return 0
    elapsed = max(0.0, float(now) - float(car.leg_started))
    return max(0, int(float(car.duration_s) - elapsed))


def waiting_seconds(person: EmpirePerson, now: float) -> int:
    """Whole seconds since this person started waiting. An unknown arrival is zero."""
    arrived = float(person.arrived or 0)
    if arrived <= 0:
        return 0
    return max(0, int(float(now) - arrived))


def _public_car(car: EmpireCar, now: float = 0) -> dict:
    if car.status not in STATUSES:
        car.status = "at_depot"
    _store_job(car)
    route = _loads(car.route_json, [])
    if len(route) >= 2:
        destination = place_name((float(route[-1][0]), float(route[-1][1])))
    else:
        destination = place_name((float(car.lat), float(car.lng)))
    trip = trip_fields(car)
    miles = _car_miles(car) + driven_miles(car, now)
    fare = _car_fare(car)
    driver = driver_share_pence(fare)
    return {
        "sign": car.sign,
        "colour": car.colour,
        "status": car.status,
        "job": car.job,
        "passenger": car.passenger,
        "lat": car.lat,
        "lng": car.lng,
        "route": route,
        "duration_s": car.duration_s,
        "leg_started": car.leg_started,
        "osrm": bool(car.osrm),
        "trail": _loads(car.trail_json, []),
        "destination": destination,
        "seconds_left": leg_seconds_left(car, now),
        "pickup": trip["pickup"],
        "drop": trip["drop"],
        "trip_pence": trip["trip_pence"],
        "journey": (car.journey or "") if car.status == "with_passenger" else "",
        "for_sale": car_is_for_sale(car),
        "miles": round(miles, 4),
        "fare_in": fare,
        "fuel_pence": fuel_pence(miles),
        "driver_share": driver,
        "company_pence": fare - driver,
    }


def _rival_rows(session: Session, username: str, now: float) -> list[dict]:
    """The other two houses, as firms. Their finished fares stay on their own game."""
    rows = []
    for other, house in SHARED_HOUSES.items():
        if other == username:
            continue
        other_game = session.get(EmpireGame, other)
        cars = _cars(session, other)
        if other_game is None or not cars:
            continue
        home = car_depot(cars[0])
        company = (other_game.company_name or "").strip() or house["company"]
        rows.append(
            {
                "firm": other,
                "company": company,
                "depot": [home[0], home[1]],
                "cars": [_public_car(car, now) for car in cars],
                "bookings": [],
                "fare_in": int(other_game.fare_in or 0),
            }
        )
    return rows


def snapshot(username: str, now: float | None = None, session: Session | None = None) -> dict:
    username = normalise_username(username)
    if now is None:
        now = time.time()
    own_session = session is None
    if session is None:
        session = SessionLocal()
    try:
        game = ensure_game(username, now, session)
        cars = _cars(session, username)
        moving = [car for car in cars if car.status != "at_depot"]
        unavailable = any(not car.osrm for car in moving)
        payload_cars = []
        fares = []
        for car in cars:
            payload_cars.append(_public_car(car, now))
            if car.status == "to_passenger" and car.fare_name:
                fares.append(
                    {
                        "name": car.fare_name,
                        "lat": car.fare_lat,
                        "lng": car.fare_lng,
                        "sign": car.sign,
                    }
                )
        by_sign = {car.sign: car for car in cars}
        if username in SHARED_HOUSES:
            for other in SHARED_HOUSES:
                if other == username:
                    continue
                for car in _cars(session, other):
                    by_sign[car.sign] = car
        else:
            for firm in FIRM_NAMES:
                for car in _fleet_cars(session, firm_key(username, firm)):
                    by_sign[car.sign] = car
        people = []
        for person in session.scalars(
            select(EmpirePerson).where(EmpirePerson.username == _people_key(username)).order_by(EmpirePerson.id)
        ).all():
            locked = by_sign.get(person.sign) if person.sign else None
            people.append(
                {
                    "name": person.name,
                    "lat": person.lat,
                    "lng": person.lng,
                    "sign": person.sign,
                    "waiting_s": waiting_seconds(person, now),
                    "seconds_left": leg_seconds_left(locked, now) if locked is not None else 0,
                    "journey": person.journey or "",
                }
            )
        done = 0.0 if game is None else float(game.miles_driven or 0)
        miles = done + sum(driven_miles(car, now) for car in cars)
        fare_in = 0 if game is None else int(game.fare_in or 0)
        lines = money_lines(len(cars), fare_in=fare_in, miles=miles)
        cash_pence = 0 if game is None else int(game.cash_pence or 0)
        balance = account_balance(game, cars, now)
        if username in SHARED_HOUSES:
            firms = _rival_rows(session, username, now)
        else:
            firms = []
            for firm in FIRM_NAMES:
                rows = _fleet_cars(session, firm_key(username, firm))
                if not rows:
                    continue
                home = car_depot(rows[0])
                bookings = [] if game is None else _public_bookings(game, firm)
                firms.append(
                    {
                        "firm": firm,
                        "depot": [home[0], home[1]],
                        "cars": [_public_car(car, now) for car in rows],
                        "bookings": bookings,
                        "fare_in": sum(int(row["pence"]) for row in bookings),
                    }
                )
        company = "" if game is None else (game.company_name or "").strip()
        return {
            "ok": True,
            "username": username,
            "company": company,
            "server_now": int(now),
            "check_s": CHECK_S,
            "trail_s": TRAIL_S,
            "osrm_unavailable": unavailable,
            "cars": payload_cars,
            "fares": fares,
            "people": people,
            "firms": firms,
            "bookings": [] if game is None else _public_trip_bookings(game),
            "loiter": public_loiter(game),
            "fare_in": lines["fare_in"],
            "cash_pence": cash_pence,
            "balance": balance,
            "driver_share": lines["driver_share"],
            "fuel": lines["fuel"],
            "standing": lines["standing"],
            "new_car": NEW_CAR_PENCE,
            "for_sale": for_sale_rows(cars),
        }
    finally:
        if own_session:
            session.close()


def game_count(username: str, session: Session) -> int:
    return int(
        session.scalar(select(func.count()).select_from(EmpireGame).where(EmpireGame.username == username)) or 0
    )


def rename_company(
    username: str,
    name: str,
    now: float | None = None,
    session: Session | None = None,
) -> str | None:
    """Store a company name. Empty and over-long names change nothing."""
    cleaned = (name or "").strip()
    if not cleaned or len(cleaned) > 48:
        return None
    username = normalise_username(username)
    own_session = session is None
    if session is None:
        session = SessionLocal()
    if now is None:
        now = time.time()
    try:
        game = ensure_game(username, now, session)
        game.company_name = cleaned
        session.commit()
        return cleaned
    finally:
        if own_session:
            session.close()


def bootstrap_shared_houses() -> None:
    """Replace users and empire rows once, when EMPIRE_RESET=1 and the flag is unset.

    The board, bus cache, devices, and location pings stay. A second boot sees the flag and leaves the rows.
    Leave EMPIRE_RESET unset after that flag is written, so a later boot cannot wipe the houses.
    """
    import os

    if os.getenv("EMPIRE_RESET") != "1":
        return
    from sqlalchemy import delete

    from .models import BoardMeta, DeviceLink, FriendLink, ShopProfile, User
    from .schemas import UserCreate

    session = SessionLocal()
    try:
        flag = session.get(BoardMeta, "empire_reset")
        if flag is not None and (flag.value or "") == RESET_FLAG:
            return
        session.execute(delete(EmpirePerson))
        session.execute(delete(EmpireCar))
        session.execute(delete(EmpireGame))
        session.execute(delete(DeviceLink))
        session.execute(delete(FriendLink))
        session.execute(delete(ShopProfile))
        session.execute(delete(User))
        if flag is not None:
            session.delete(flag)
        session.commit()
    finally:
        session.close()
    for username in SHARED_HOUSES:
        User.create(UserCreate(username=username, password="Hello123!"))
        ensure_game(username)
    session = SessionLocal()
    try:
        session.add(BoardMeta(key="empire_reset", value=RESET_FLAG))
        session.commit()
    finally:
        session.close()
    log.info("shared world reset for michael, noah, reeve")
