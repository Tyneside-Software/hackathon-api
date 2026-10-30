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
from .doors import depot_door, named_door, street_address
from .models import EmpireCar, EmpireGame, EmpirePerson

log = logging.getLogger("hackathon-api")

DEPOT = (55.0367, -1.5075)
EMPTY_MOVE_MILES = 2
EMPTY_MOVE_KM = EMPTY_MOVE_MILES * 1.609344
# Newcastle. 50 miles: 50 * 1.609344 km.
CIRCLE_CENTRE = (54.9783, -1.6178)
CIRCLE_RADIUS_KM = 80.467
LOITER_MIN_KM = 0.2
LOITER_MAX_KM = 8.0
EARTH_KM = 6371.0
KM_PER_MILE = 1.609344
MPH = 30.0
CLAIM_MINUTES = 10
PASSENGER_WAIT_S = CLAIM_MINUTES * 60
CLAIM_RADIUS_KM = (CLAIM_MINUTES / 60.0) * MPH * KM_PER_MILE
STANDING_PENCE = 1500
FUEL_PENCE_PER_MILE = 20
NEW_CAR_PENCE = 50_000
SELL_CAR_PENCE = 40_000
# The old car price. A leaked 800000 is still not a fare. A real trip may cost 50000.
NOT_A_FARE_PENCE = 800_000
CHECK_S = 10
TRAIL_S = 600
DEPOT_PAUSE_S = 4
BREAK_S = 180
WORK_DAY_S = 12 * 3600
WORK_WEEK_S = 60 * 3600
STINT_S = 4 * 3600
BREAK_NEED_S = 3600
MIN_WORKING = 2
DUTY_TICK_CAP_S = 5.0
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


def _duty(car: EmpireCar) -> dict:
    try:
        got = json.loads(getattr(car, "duty_json", "") or "{}")
    except json.JSONDecodeError:
        return {}
    return got if isinstance(got, dict) else {}


def _save_duty(car: EmpireCar, duty: dict) -> None:
    car.duty_json = json.dumps(duty)


def _hours_resting(car: EmpireCar) -> bool:
    """True while this driver is on the mandatory break or asleep at home."""
    return str(_duty(car).get("kind") or "") in ("break", "day", "week")


def _stored_home(car: EmpireCar) -> tuple[float, float] | None:
    duty = _duty(car)
    lat = float(duty.get("home_lat") or 0)
    lng = float(duty.get("home_lng") or 0)
    if lat == 0.0 and lng == 0.0:
        return None
    return (lat, lng)


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
    """Start the leg where the car already is. Do not snap the start back to Shiremoor.

    A road route often finishes on the nearest road, short of the hail. The stored leg
    still ends on the exact point, or the next tick treats the hunt as a far empty move
    and starts it again from the same spot.
    """
    route, duration, osrm = build_route(start, end)
    door = [float(end[0]), float(end[1])]
    if len(route) < 2:
        route = [[float(start[0]), float(start[1])], door]
    else:
        route[0] = [float(start[0]), float(start[1])]
        last = route[-1]
        gap = haversine_km((float(last[0]), float(last[1])), (door[0], door[1]))
        if gap > 0.02:
            route.append(door)
        else:
            route[-1] = door
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
# Roles for the places already published above. A home is not in this table.
PLACE_ROLES = {
    "Cobalt": ("work",),
    "Regent Centre": ("work",),
    "Grey's Monument": ("work",),
    "the Quayside": ("work", "night"),
    "Haymarket": ("station", "work", "arrival"),
    "Four Lane Ends": ("station", "arrival"),
    "Central Station": ("station", "arrival"),
    "Silverlink": ("shop",),
    "Royal Quays": ("shop",),
    "Whitley Bay": ("coast", "night"),
    "Spanish City": ("coast", "night"),
    "Cullercoats": ("coast",),
    "Tynemouth": ("coast", "night"),
    "North Shields": ("coast",),
    "Osborne Road": ("night",),
    "North Tyneside General at Rake Lane": ("hospital",),
    "the RVI": ("hospital",),
    "the Freeman": ("hospital",),
    "Newcastle Airport": ("airport", "arrival"),
}
# Real sites across the circle. The coordinate is the building, not a town centre guess.
# Homes stay out of this list and are scattered around the town where that journey starts.
SPOTS = (
    ("Teesside Airport", 54.50941, -1.42479, ("airport", "arrival")),
    ("Durham Station", 54.77996, -1.58197, ("station", "arrival")),
    ("Darlington Station", 54.52067, -1.54666, ("station", "arrival")),
    ("Middlesbrough Station", 54.57915, -1.23451, ("station", "arrival")),
    ("Sunderland Station", 54.90554, -1.38237, ("station", "arrival")),
    ("Hexham Station", 54.97303, -2.09476, ("station", "arrival")),
    ("Morpeth Station", 55.16274, -1.67961, ("station", "arrival")),
    ("Chester-le-Street Station", 54.85454, -1.57825, ("station", "arrival")),
    ("Hartlepool Station", 54.68666, -1.20764, ("station", "arrival")),
    ("Alnmouth Station", 55.39285, -1.63663, ("station", "arrival")),
    ("QE Gateshead", 54.93883, -1.58015, ("hospital",)),
    ("Sunderland Royal", 54.90277, -1.40871, ("hospital",)),
    ("James Cook", 54.55200, -1.20853, ("hospital",)),
    ("University Hospital North Durham", 54.78872, -1.59318, ("hospital",)),
    ("NSECH Cramlington", 55.07490, -1.56871, ("hospital",)),
    ("Wansbeck General", 55.18412, -1.54516, ("hospital",)),
    ("Darlington Memorial", 54.53066, -1.56364, ("hospital",)),
    ("University Hospital Hartlepool", 54.70211, -1.22803, ("hospital",)),
    ("Metrocentre", 54.95889, -1.66493, ("shop",)),
    ("Eldon Square", 54.97418, -1.61455, ("shop",)),
    ("Dalton Park", 54.81453, -1.37452, ("shop",)),
    ("Teesside Park", 54.55446, -1.27227, ("shop",)),
    ("The Bridges", 54.90616, -1.38314, ("shop",)),
    ("Quorum Business Park", 55.01897, -1.58464, ("work",)),
    ("Newcastle Helix", 54.97267, -1.62767, ("work",)),
    ("Team Valley", 54.93392, -1.61900, ("work",)),
    ("Nissan", 54.90784, -1.48927, ("work",)),
    ("NETPark", 54.67019, -1.45384, ("work",)),
    ("Aykley Heads", 54.78937, -1.58767, ("work",)),
    ("Wilton", 54.58946, -1.11234, ("work",)),
    ("Port of Tyne", 54.99250, -1.45222, ("work",)),
    ("Newcastle University", 54.98018, -1.61468, ("university",)),
    ("Northumbria University", 54.97786, -1.60707, ("university",)),
    ("University of Sunderland", 54.90439, -1.39179, ("university",)),
    ("Teesside University", 54.56953, -1.23554, ("university",)),
    ("Durham Cathedral", 54.77326, -1.57633, ("university", "outing")),
    ("St James' Park", 54.97547, -1.62187, ("stadium",)),
    ("Stadium of Light", 54.91440, -1.38826, ("stadium",)),
    ("Riverside Stadium", 54.57831, -1.21692, ("stadium",)),
    ("Kingston Park", 55.01867, -1.67227, ("stadium",)),
    ("North Shields Ferry", 55.00377, -1.44406, ("ferry",)),
    ("Vermont Hotel", 54.96914, -1.60956, ("hotel",)),
    ("Hilton Newcastle Gateshead", 54.96611, -1.60647, ("hotel",)),
    ("Sandman Signature", 54.97359, -1.62196, ("hotel",)),
    ("Maldron Hotel", 54.97162, -1.61629, ("hotel",)),
    ("Grand Hotel Tynemouth", 55.02221, -1.42356, ("hotel",)),
    ("Ramside Hall", 54.79636, -1.51995, ("hotel",)),
    ("Rockliffe Hall", 54.47858, -1.54582, ("hotel",)),
    ("Bigg Market", 54.97127, -1.61399, ("night",)),
    ("Walkergate", 54.77819, -1.57608, ("night",)),
    ("Linthorpe Road", 54.56068, -1.24269, ("night",)),
    ("Seaburn", 54.93395, -1.36962, ("coast",)),
    ("Redcar Esplanade", 54.61879, -1.06403, ("coast",)),
    ("Seaham Marina", 54.83794, -1.32808, ("coast",)),
    ("Bamburgh Castle", 55.60944, -1.71127, ("coast", "outing")),
    ("Beamish Museum", 54.88573, -1.65980, ("outing",)),
    ("Angel of the North", 54.91412, -1.58952, ("outing",)),
    ("Alnwick Castle", 55.41547, -1.70649, ("outing",)),
)
# name, origin role, destination role, weights for morning/day/evening/night/small hours,
# shortest km, longest km, bands that swap the ends, and "half" or "late" when either way is normal.
# "home" is a numbered street in a town. Every other role is a named building.
# Each end is a first line, a town, and a postcode. 800000 is not a weight.
# A zero stays zero at that hour. _week_weight raises the kinds that already belong to the day.
JOURNEY_KINDS = (
    ("commute", "home", "work", (12, 2, 10, 0, 2), 2.0, 26.0, (2,), ""),
    ("station", "home", "station", (4, 3, 4, 3, 2), 0.8, 18.0, (2, 3), ""),
    ("airport", "home", "airport", (3, 3, 3, 3, 2), 3.0, 75.0, (), "half"),
    ("hospital", "home", "hospital", (3, 4, 3, 2, 4), 1.2, 30.0, (), "late"),
    ("university", "home", "university", (4, 3, 2, 0, 0), 1.0, 28.0, (2,), ""),
    ("shopping", "home", "shop", (1, 6, 3, 0, 0), 1.2, 22.0, (), ""),
    ("coast", "home", "coast", (1, 5, 4, 1, 0), 2.5, 45.0, (), ""),
    ("day out", "home", "outing", (0, 4, 2, 0, 0), 3.0, 55.0, (), ""),
    ("night out", "night", "home", (0, 1, 6, 16, 3), 0.8, 16.0, (2,), ""),
    ("hotel", "hotel", "arrival", (2, 2, 2, 2, 2), 0.35, 55.0, (), "half"),
    ("shift", "home", "work", (2, 1, 2, 2, 5), 2.0, 28.0, (3, 4), ""),
    ("short hop", "home", "home", (3, 4, 3, 2, 2), 0.4, 3.5, (), ""),
    ("match", "stadium", "station", (0, 1, 3, 2, 0), 0.35, 18.0, (), "half"),
    ("ferry", "home", "ferry", (1, 2, 2, 1, 1), 1.5, 40.0, (), "half"),
)
# Where people live, in approximate thousands, and how widely a hail spreads around that town.
# The weights follow the built-up population across the circle. The open country stays thin.
SETTLEMENTS = (
    ("Newcastle", 54.978, -1.614, 293, 3.2),
    ("Gateshead", 54.955, -1.610, 196, 3.0),
    ("Sunderland", 54.906, -1.390, 275, 3.6),
    ("South Shields", 54.999, -1.432, 75, 2.2),
    ("Jarrow", 54.982, -1.489, 40, 1.8),
    ("North Shields", 55.009, -1.455, 40, 1.8),
    ("Tynemouth", 55.017, -1.430, 20, 1.4),
    ("Whitley Bay", 55.039, -1.455, 35, 1.8),
    ("Wallsend", 54.991, -1.534, 45, 1.8),
    ("Gosforth", 55.007, -1.620, 40, 1.6),
    ("Heaton", 54.988, -1.570, 30, 1.4),
    ("Shiremoor", 55.035, -1.510, 12, 1.4),
    ("Killingworth", 55.033, -1.567, 18, 1.4),
    ("Cramlington", 55.087, -1.585, 30, 1.8),
    ("Blyth", 55.127, -1.510, 38, 2.0),
    ("Ashington", 55.181, -1.570, 28, 1.6),
    ("Bedlington", 55.133, -1.580, 18, 1.4),
    ("Morpeth", 55.167, -1.688, 14, 1.6),
    ("Ponteland", 55.048, -1.745, 11, 1.4),
    ("Hexham", 54.971, -2.101, 12, 1.6),
    ("Prudhoe", 54.961, -1.849, 12, 1.4),
    ("Washington", 54.900, -1.520, 55, 2.4),
    ("Chester-le-Street", 54.859, -1.574, 25, 1.6),
    ("Durham", 54.776, -1.575, 48, 2.2),
    ("Stanley", 54.868, -1.698, 16, 1.4),
    ("Consett", 54.850, -1.832, 22, 1.6),
    ("Peterlee", 54.760, -1.340, 22, 1.6),
    ("Seaham", 54.840, -1.350, 20, 1.4),
    ("Hartlepool", 54.686, -1.212, 92, 2.4),
    ("Alnwick", 55.413, -1.706, 8, 1.6),
    ("Amble", 55.333, -1.578, 6, 1.3),
    ("Rothbury", 55.309, -1.910, 2, 1.2),
    ("Wooler", 55.547, -2.015, 2, 1.3),
    ("Seahouses", 55.581, -1.655, 2, 1.2),
    ("Belford", 55.600, -1.829, 1, 1.1),
    ("Bellingham", 55.144, -2.254, 1, 1.1),
    ("Haltwhistle", 54.971, -2.456, 3, 1.3),
    ("Allendale", 54.897, -2.258, 1, 1.1),
    ("Corbridge", 54.974, -2.017, 4, 1.2),
    ("Alston", 54.812, -2.441, 1, 1.1),
    ("Brampton", 54.941, -2.735, 4, 1.3),
    ("Stanhope", 54.747, -2.006, 1, 1.2),
    ("Bishop Auckland", 54.656, -1.676, 25, 1.8),
    ("Crook", 54.713, -1.749, 8, 1.4),
    ("Spennymoor", 54.700, -1.602, 18, 1.6),
    ("Ferryhill", 54.688, -1.551, 9, 1.4),
    ("Sedgefield", 54.653, -1.450, 5, 1.3),
    ("Newton Aycliffe", 54.614, -1.569, 25, 1.8),
    ("Darlington", 54.525, -1.553, 93, 2.4),
    ("Barnard Castle", 54.544, -1.925, 5, 1.4),
    ("Stockton", 54.568, -1.319, 82, 2.2),
    ("Billingham", 54.605, -1.290, 35, 1.8),
    ("Middlesbrough", 54.574, -1.235, 140, 2.8),
    ("Yarm", 54.508, -1.355, 8, 1.3),
    ("Redcar", 54.616, -1.060, 35, 1.6),
    ("Guisborough", 54.535, -1.056, 16, 1.4),
    ("Northallerton", 54.338, -1.436, 16, 1.5),
    ("Richmond", 54.404, -1.735, 8, 1.4),
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
    for name, lat, lng, _roles in SPOTS:
        if (round(lat, 5), round(lng, 5)) == key:
            return name
    return ""


def _exact_place_name(point: tuple[float, float]) -> str:
    """A published pin only. The depot radius is not a building."""
    key = (round(float(point[0]), 5), round(float(point[1]), 5))
    for name, lat, lng in PLACES:
        if (round(lat, 5), round(lng, 5)) == key:
            return name
    for name, lat, lng, _roles in SPOTS:
        if (round(lat, 5), round(lng, 5)) == key:
            return name
    return ""


def _nearest_settlement(point: tuple[float, float]) -> str:
    best = SETTLEMENTS[0][0]
    best_km = haversine_km(point, (SETTLEMENTS[0][1], SETTLEMENTS[0][2]))
    for name, lat, lng, _pop, _spread in SETTLEMENTS[1:]:
        distance = haversine_km(point, (lat, lng))
        if distance < best_km:
            best = name
            best_km = distance
    return best


def postal_address(point: tuple[float, float]) -> dict[str, str]:
    """First line, town, and postcode for this point.

    A published building keeps that door. The yard is the Shiremoor depot.
    Anything else is a numbered street in the nearest town. (0, 0) is unset.
    """
    lat = float(point[0])
    lng = float(point[1])
    if lat == 0.0 and lng == 0.0:
        return {"line": "", "city": "", "postcode": ""}
    name = _exact_place_name((lat, lng))
    if name:
        door = named_door(name)
        if door is not None:
            return door
    if haversine_km((lat, lng), DEPOT) <= 0.08:
        return depot_door()
    return street_address(_nearest_settlement((lat, lng)), lat, lng)


def _filled_door(point: tuple[float, float] | None) -> dict[str, str]:
    """A complete door. An unset point uses the yard, so the panel is never blank."""
    if point is None or (float(point[0]) == 0.0 and float(point[1]) == 0.0):
        return postal_address(DEPOT)
    door = postal_address(point)
    if not door["line"] or not door["city"] or not door["postcode"]:
        return postal_address(DEPOT)
    return door


def _car_doors(car: EmpireCar) -> tuple[dict[str, str], dict[str, str]]:
    """Origin and destination for the journey this car is on, or the leg it is driving.

    A passenger job uses the hail and the drop. A rest uses the sleep door.
    A parked car is here, at one door.
    """
    ends = _passenger_ends(car)
    if ends is not None:
        return _filled_door(ends[0]), _filled_door(ends[1])
    route = _loads(car.route_json, [])
    if _hours_resting(car):
        home = _stored_home(car)
        start = (float(route[0][0]), float(route[0][1])) if route else (float(car.lat), float(car.lng))
        return _filled_door(start), _filled_door(home if home is not None else car_depot(car))
    if len(route) >= 2:
        start = (float(route[0][0]), float(route[0][1]))
        end = (float(route[-1][0]), float(route[-1][1]))
        return _filled_door(start), _filled_door(end)
    here = (float(car.lat), float(car.lng))
    if here == (0.0, 0.0):
        here = car_depot(car)
    door = _filled_door(here)
    return door, door


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


def _london_local(now: float) -> datetime:
    """Europe/London civil time. The weekly clock uses this, not the machine zone."""
    utc = datetime.fromtimestamp(now, tz=timezone.utc)
    return utc + timedelta(hours=_london_offset_hours(utc))


def _spell_key(now: float) -> str:
    local = _london_local(now)
    return f"{local.date().isoformat()}|{_band_index(now)}"


# Standing hails, Monday..Sunday, then morning/day/evening/night/small hours.
# Spread across the 50-mile circle by the towns, not grown with the fleet.
# The quietest hour is the baseline. Weekday mornings, Friday night, and Saturday sit much higher.
WEEK_DEMAND = (
    (800, 420, 760, 380, 280),
    (820, 420, 760, 380, 280),
    (820, 430, 760, 390, 280),
    (800, 440, 780, 440, 300),
    (760, 460, 820, 720, 460),
    (300, 640, 700, 780, 520),
    (280, 600, 520, 400, 280),
)


def spell_target(username: str, now: float) -> int:
    """How many hails stand on the map during this spell.

    Every hour of the week keeps the baseline. A rush or a weekend night adds many more.
    A small fleet never empties the stock. A fleet near the peak leaves some taxis idle
    until a collected or expired hail frees a slot.
    """
    local = _london_local(now)
    band = _band_index(now)
    rng = random.Random(_engine_seed(username, _spell_key(now) + "|target"))
    return WEEK_DEMAND[local.weekday()][band] + rng.randrange(0, 20)


def _pick_name(rng: random.Random, names: tuple[str, ...]) -> str:
    weights = [HOME_WEIGHTS.get(name, 1) for name in names]
    return rng.choices(list(names), weights=weights, k=1)[0]


def _on_land(lat: float, lng: float) -> bool:
    """Coarse east coast. The sea sits east of these longitudes. A hail does not stand in the water."""
    if lat >= 55.62:
        coast = -1.95
    elif lat >= 55.50:
        coast = -1.63
    elif lat >= 55.35:
        coast = -1.58
    elif lat >= 55.20:
        coast = -1.54
    elif lat >= 55.08:
        coast = -1.48
    elif lat >= 54.97:
        coast = -1.40
    elif lat >= 54.85:
        coast = -1.36
    elif lat >= 54.75:
        coast = -1.30
    elif lat >= 54.66:
        coast = -1.16
    elif lat >= 54.58:
        coast = -1.04
    else:
        coast = -0.98
    return lng <= coast


def _pool(role: str) -> tuple[tuple[str, float, float], ...]:
    """Named buildings in one journey pool. Homes are not in a pool."""
    found: list[tuple[str, float, float]] = []
    for name, lat, lng in PLACES:
        if role in PLACE_ROLES.get(name, ()):
            found.append((name, float(lat), float(lng)))
    for name, lat, lng, roles in SPOTS:
        if role in roles:
            found.append((name, float(lat), float(lng)))
    return tuple(found)


def _offset_km(lat: float, lng: float, north_km: float, east_km: float) -> tuple[float, float]:
    return (
        lat + north_km / 111.0,
        lng + east_km / (111.0 * max(0.2, math.cos(math.radians(lat)))),
    )


def _scatter_around(rng: random.Random, lat: float, lng: float, spread_km: float) -> tuple[float, float] | None:
    """A residential point around a town. The building pins are not moved."""
    for _ in range(24):
        angle = rng.random() * (2 * math.pi)
        unit = max(rng.random(), 1e-9)
        radius_km = min(spread_km * 2.2, spread_km * math.sqrt(-2.0 * math.log(unit)))
        radius_km = max(radius_km, 0.15)
        point = _offset_km(lat, lng, radius_km * math.cos(angle), radius_km * math.sin(angle))
        if inside_circle(point) and _on_land(point[0], point[1]):
            return point
    return None


def _home_near(
    rng: random.Random,
    anchor: tuple[float, float],
    min_km: float,
    max_km: float,
) -> tuple[float, float] | None:
    """A street in a town that can actually reach this building. Bigger towns weigh more."""
    span = max(8.0, (max_km - min_km) * 0.65)
    rows: list[tuple[float, float, float]] = []
    weights: list[float] = []
    for _name, lat, lng, pop, spread in SETTLEMENTS:
        dist = haversine_km((lat, lng), anchor)
        if dist > max_km + spread or dist + spread < min_km:
            continue
        rows.append((lat, lng, spread))
        weights.append(max(pop, 1) * math.exp(-max(0.0, dist - min_km) / span))
    if not rows:
        return None
    lat, lng, spread = rng.choices(rows, weights=weights, k=1)[0]
    for _ in range(16):
        point = _scatter_around(rng, lat, lng, spread)
        if point is None:
            continue
        dist = haversine_km(point, anchor)
        if min_km <= dist <= max_km and dist > 0.25:
            return point
    return None


def _short_hop(rng: random.Random, min_km: float, max_km: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Two streets in the same town. A short hop does not cross the county."""
    weights = [row[3] for row in SETTLEMENTS]
    for _attempt in range(40):
        _name, lat, lng, _pop, spread = rng.choices(list(SETTLEMENTS), weights=weights, k=1)[0]
        local = min(spread, 1.8)
        first = _scatter_around(rng, lat, lng, local)
        second = _scatter_around(rng, lat, lng, local)
        if first is None or second is None:
            continue
        if min_km <= haversine_km(first, second) <= max_km:
            return first, second
    centre = (SETTLEMENTS[0][1], SETTLEMENTS[0][2])
    return centre, _offset_km(centre[0], centre[1], 0.6, 0.4)


def _ends_for(
    rng: random.Random,
    origin_role: str,
    dest_role: str,
    min_km: float,
    max_km: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """One pair for this kind. The building stays put. The home is chosen to fit the distance."""
    if origin_role == "home" and dest_role == "home":
        return _short_hop(rng, min_km, max_km)
    for relax in (1.0, 1.6, 3.0):
        lo = min_km / relax
        hi = max_km * relax
        for _attempt in range(40):
            pickup: tuple[float, float] | None = None
            drop: tuple[float, float] | None = None
            if origin_role == "home":
                pin = rng.choice(_pool(dest_role))
                home = _home_near(rng, (pin[1], pin[2]), lo, hi)
                if home is None:
                    continue
                pickup, drop = home, (pin[1], pin[2])
            elif dest_role == "home":
                pin = rng.choice(_pool(origin_role))
                home = _home_near(rng, (pin[1], pin[2]), lo, hi)
                if home is None:
                    continue
                pickup, drop = (pin[1], pin[2]), home
            else:
                origin = rng.choice(_pool(origin_role))
                dest = rng.choice(_pool(dest_role))
                if origin[0] == dest[0]:
                    continue
                pickup, drop = (origin[1], origin[2]), (dest[1], dest[2])
            dist = haversine_km(pickup, drop)
            if dist <= 0.25 or dist < lo or dist > hi:
                continue
            if not inside_circle(pickup) or not inside_circle(drop):
                continue
            if not _on_land(pickup[0], pickup[1]) or not _on_land(drop[0], drop[1]):
                continue
            return pickup, drop
    return place_point("Grey's Monument"), place_point("Central Station")


def _is_saturday(now: float) -> bool:
    return _london_local(now).weekday() == 5


def _week_weight(name: str, base: int, band: int, weekday: int) -> int:
    """The hour weight, reshaped by the day. A zero stays zero.

    Weekday mornings and evenings push the commute. Friday and Saturday push the night out.
    Saturday and Sunday push shops, the coast, and a day out, and quiet the office rush.
    """
    if base <= 0:
        return 0
    weekend = weekday >= 5
    friday = weekday == 4
    if name == "commute":
        if weekend:
            return 1 if band in (0, 2) else min(base, 2)
        if band in (0, 2):
            return base + 10
        return base
    if name == "university" and weekend:
        return 1
    if name == "shopping" and band == 1:
        if weekday == 5:
            return base + 12
        if weekday == 6:
            return base + 6
    if name == "coast" and weekend and band in (1, 2):
        return base + 6
    if name == "day out" and weekend and band in (1, 2):
        return base + 8
    if name == "night out":
        if friday and band in (2, 3):
            return base + 14
        if weekday == 5 and band in (2, 3, 4):
            return base + 16
        if weekday == 6 and band in (2, 3):
            return max(1, base // 3)
    if name == "match":
        if weekday == 5:
            return base + 8
        if weekday == 6 and band in (1, 2):
            return base + 5
        if weekday == 2 and band in (2, 3):
            return base + 2
    if name == "airport" and (weekend or friday):
        return base + 2
    if name == "shift" and band in (3, 4):
        return base + 2
    return base


def draw_journey(username: str, now: float, salt: str) -> tuple[str, tuple[float, float], tuple[float, float]]:
    """Draw one journey kind, then a fitting origin and destination.

    The hour changes which kinds are busy, and the day raises the ones that belong to it.
    A commute in the evening runs back home. An airport, hotel, or ferry trip runs either way.
    A home is a numbered street in a likely town. A station, hospital, hotel, or workplace stays on that building.
    postal_address turns either end into a first line, a town, and a postcode.
    """
    rng = random.Random(_engine_seed(username, f"{_spell_key(now)}|{salt}|journey"))
    local = _london_local(now)
    band = _band_index(now)
    weekday = local.weekday()
    weights = [_week_weight(kind[0], kind[3][band], band, weekday) for kind in JOURNEY_KINDS]
    if sum(weights) <= 0:
        weights = [1] * len(JOURNEY_KINDS)
    kind_name, origin_role, dest_role, _weights, min_km, max_km, reverse, flip = rng.choices(
        list(JOURNEY_KINDS), weights=weights, k=1
    )[0]
    if band in reverse:
        origin_role, dest_role = dest_role, origin_role
    if flip == "half" or (flip == "late" and band == 4):
        if rng.random() < 0.5:
            origin_role, dest_role = dest_role, origin_role
    pickup, drop = _ends_for(rng, origin_role, dest_role, min_km, max_km)
    return kind_name, pickup, drop


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


def _release_unclaimed(session: Session, username: str, now: float) -> None:
    """A hail nobody has taken leaves the map. A taxi already on the way keeps its passenger."""
    removed = False
    for person in _people(session, username):
        if person.sign:
            continue
        arrived = float(person.arrived or 0)
        if arrived <= 0:
            continue
        if float(now) - arrived < PASSENGER_WAIT_S:
            continue
        session.delete(person)
        removed = True
    if removed:
        session.flush()


def _ensure_waiting_person(game: EmpireGame, session: Session, now: float) -> None:
    """Keep this spell's standing stock of hails filled.

    A collected or expired hail frees a slot and the next call draws another.
    Claimed people who have not been collected still count toward the cap.
    A bad draw is skipped. The cap is the demand, not the size of the fleet.
    """
    _release_unclaimed(session, game.username, now)
    target = spell_target(game.username, now)
    have = len(_people(session, game.username))
    added = False
    attempts = 0
    while have < target and attempts < target + 64:
        attempts += 1
        index = int(game.name_index or 0)
        kind, pickup, drop = draw_journey(game.username, now, f"person-{index}")
        game.name_index = index + 1
        if not inside_circle(pickup) or not inside_circle(drop) or pickup == drop:
            continue
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
        have += 1
        added = True
    if added:
        session.flush()


def _can_take_a_hail(car: EmpireCar) -> bool:
    """A taxi at the depot, or one already driving toward a hail, may take it once it is in range.

    Collecting, carrying, and returning stay on that leg. Seeing a hail is not claiming it.
    A driver on a break or asleep does not take one.
    """
    if _hours_resting(car):
        return False
    if not car_may_depart(car) or car.passenger or car.fare_name:
        return False
    return car.status == "at_depot" or car.status == "to_better_spot"


def _claim_closest(people: list[EmpirePerson], cars: list[EmpireCar], taken: set[str]) -> None:
    """One free car takes the closest person who is still within ten minutes.

    A hail further away stays on the map for a taxi to drive toward. Equal distances keep
    the older person and the lower sign. A car on a collecting, carrying, or returning leg is not here.
    """
    pending = [person for person in people if not person.sign]
    free = [car for car in cars if car.sign not in taken and _can_take_a_hail(car)]
    while pending and free:
        best_rank = None
        best_person = None
        best_car = None
        for person in pending:
            pickup = (float(person.lat), float(person.lng))
            for car in free:
                distance = haversine_km(car_position(car), pickup)
                if distance > CLAIM_RADIUS_KM:
                    continue
                rank = (
                    distance,
                    int(person.id or 0),
                    int(car.sign[1:]),
                    car.sign,
                )
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    best_person = person
                    best_car = car
        if best_person is None or best_car is None:
            return
        best_person.sign = best_car.sign
        taken.add(best_car.sign)
        pending.remove(best_person)
        free.remove(best_car)


def _allocate_one_free_taxi(game: EmpireGame, session: Session) -> None:
    """Each waiting person is claimed by the closest free taxi. A car on a leg is left alone.

    People left over do not add a car. Two people do not claim the same taxi.
    A loiter patch does not keep a free taxi off a hail.
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
        key = firm_key(game.username, firm)
        firm_cars.extend(_fleet_cars(session, key))
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
    if _hours_resting(car):
        return "on break"
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
    if pence == NOT_A_FARE_PENCE:
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
        if pence == NOT_A_FARE_PENCE:
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
    if pence == NOT_A_FARE_PENCE:
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
        if _hours_resting(car):
            _sit_at_home(car, now)
            return
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


def _hail_point(person: EmpirePerson) -> tuple[float, float]:
    return (float(person.lat), float(person.lng))


def _route_end(car: EmpireCar) -> tuple[float, float] | None:
    route = _loads(car.route_json, [])
    if len(route) < 2:
        return None
    return (float(route[-1][0]), float(route[-1][1]))


# A driving route finishes on the nearest road. That can sit well short of the door.
ROAD_HAIL_KM = 2.0


def _near_hail(point: tuple[float, float], person: EmpirePerson) -> bool:
    return haversine_km(point, _hail_point(person)) <= ROAD_HAIL_KM


def _far_leg_may_continue(car: EmpireCar, game: EmpireGame, end: tuple[float, float]) -> bool:
    """A hunt, or a drive into the loiter patch, may be longer than two miles."""
    if _leg_follows_a_hail(car, end):
        return True
    home = _stored_home(car)
    if _hours_resting(car) and home is not None and haversine_km(end, home) <= ROAD_HAIL_KM:
        return True
    draw_as = car.username if (car.firm or "") else None
    patch = loiter_for_fleet(game, draw_as)
    if patch is None:
        return False
    centre, radius = patch
    return haversine_km(end, centre) <= radius + ROAD_HAIL_KM


def _leg_follows_a_hail(car: EmpireCar, end: tuple[float, float]) -> bool:
    """True when this empty leg is the drive toward a hail this taxi may still take.

    A wander of more than two miles still stops. A hunt across the circle does not.
    """
    session = object_session(car)
    if session is None:
        return False
    for person in _people(session, _people_key(car.username)):
        if person.sign and person.sign != car.sign:
            continue
        if _near_hail(end, person):
            return True
    return False


def _open_hail_at(car: EmpireCar, people: list[EmpirePerson]) -> EmpirePerson | None:
    """The unclaimed hail this hunt is already driving to, if that hail is still waiting."""
    end = _route_end(car)
    if end is None:
        return None
    for person in people:
        if person.sign:
            continue
        if _near_hail(end, person):
            return person
    return None


def _nearest_open_hail(
    origin: tuple[float, float],
    people: list[EmpirePerson],
    reserved: set[int],
) -> EmpirePerson | None:
    """The closest unclaimed hail no other idle taxi in this fleet is already hunting."""
    best: EmpirePerson | None = None
    best_rank: tuple[float, int] | None = None
    for person in people:
        if person.sign or int(person.id or 0) in reserved:
            continue
        rank = (haversine_km(origin, _hail_point(person)), int(person.id or 0))
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best = person
    return best


def _drive_toward_hail(car: EmpireCar, game: EmpireGame, person: EmpirePerson, now: float) -> None:
    """Drive toward a hail the taxi can see. The claim waits until the car is in range."""
    start = car_position(car, car_depot(car))
    car.passenger = ""
    car.fare_name = ""
    car.fare_lat = 0.0
    car.fare_lng = 0.0
    car.drop_lat = 0.0
    car.drop_lng = 0.0
    _begin(car, game, start, _hail_point(person), now, "to_better_spot")
    _store_job(car)


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
            if haversine_km(here, end) > EMPTY_MOVE_KM and not _far_leg_may_continue(car, game, end):
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


def _clear_empty(car: EmpireCar) -> None:
    car.passenger = ""
    car.fare_name = ""
    car.fare_lat = 0.0
    car.fare_lng = 0.0
    car.drop_lat = 0.0
    car.drop_lng = 0.0


def _london_stamp(local: datetime) -> float:
    """Unix time for a naive Europe/London civil time. Midnight is never the clock-change hour."""
    wall = local.replace(tzinfo=None)
    for offset in (0, 1):
        utc = (wall - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
        if _london_offset_hours(utc) == offset:
            return utc.timestamp()
    return wall.replace(tzinfo=timezone.utc).timestamp()


def _next_london_midnight(now: float) -> float:
    local = _london_local(now)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return _london_stamp(nxt)


def _next_london_monday(now: float) -> float:
    local = _london_local(now)
    days = (7 - local.weekday()) % 7
    if days == 0:
        days = 7
    monday = (local + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return _london_stamp(monday)


def _week_key(now: float) -> str:
    local = _london_local(now)
    return f"{local.isocalendar().year}-W{local.isocalendar().week:02d}"


def _on_passenger_leg(car: EmpireCar) -> bool:
    if car.passenger or car.fare_name:
        return True
    return car.status in ("to_passenger", "with_passenger")


def _release_uncollected(car: EmpireCar) -> None:
    """A driver going home drops a hail they have not reached yet."""
    if car.passenger or car.fare_name:
        return
    person = _allocated_person(car)
    if person is None:
        return
    person.sign = ""


def _sleep_home_point(
    username: str,
    sign: str,
    centre: tuple[float, float],
    radius_km: float,
    taken: tuple[tuple[float, float], ...],
) -> tuple[float, float]:
    """A stable point inside the loiter patch. The same taxi keeps the same door."""
    last = centre
    for attempt in range(24):
        rng = random.Random(_engine_seed(username, f"sleep-home|{sign}|{attempt}"))
        bearing = rng.random() * 360.0
        dist_km = max(0.04, radius_km * 0.9 * math.sqrt(rng.random()))
        point = destination(centre, bearing, dist_km / KM_PER_MILE)
        last = point
        if _inside_loiter(point, centre, radius_km) and not _point_taken(point, taken):
            return point
    number = _sign_index(sign) or 1
    for step in range(36):
        bearing = float((step * 37 + number * 17) % 360)
        point = destination(centre, bearing, max(0.03, radius_km * 0.45) / KM_PER_MILE)
        last = point
        if _inside_loiter(point, centre, radius_km) and not _point_taken(point, taken):
            return point
    return last


def _depot_home_point(username: str, sign: str, depot: tuple[float, float]) -> tuple[float, float]:
    """A stable door near the depot when this fleet has no loiter patch yet."""
    rng = random.Random(_engine_seed(username, f"sleep-home|{sign}|depot"))
    point = destination(depot, rng.random() * 360.0, 0.2 + rng.random() * 0.5)
    if inside_circle(point):
        return point
    return depot


def _ensure_sleep_home(
    car: EmpireCar,
    game: EmpireGame,
    draw_as: str | None,
    taken: list[tuple[float, float]],
) -> tuple[float, float]:
    """Home is inside the loiter patch. A moved patch gets a new door."""
    duty = _duty(car)
    home = _stored_home(car)
    patch = loiter_for_fleet(game, draw_as)
    if patch is not None:
        centre, radius = patch
        if home is None or not _inside_loiter(home, centre, radius):
            home = _sleep_home_point(car.username, car.sign, centre, radius, tuple(taken))
    elif home is None:
        home = _depot_home_point(car.username, car.sign, car_depot(car))
    if home is not None:
        duty["home_lat"] = home[0]
        duty["home_lng"] = home[1]
        _save_duty(car, duty)
        taken.append(home)
    return home or car_depot(car)


def _roll_duty_calendar(duty: dict, now: float) -> None:
    """A new London day or week starts the counters again. The drive home still finishes."""
    day = _london_local(now).date().isoformat()
    week = _week_key(now)
    if duty.get("day") != day:
        duty["day"] = day
        duty["day_s"] = 0.0
    if duty.get("week") != week:
        duty["week"] = week
        duty["week_s"] = 0.0


def _at_sleep_home(car: EmpireCar) -> bool:
    home = _stored_home(car)
    if home is None:
        return False
    return haversine_km(car_position(car), home) <= 0.08


def _clear_finished_rest(car: EmpireCar, duty: dict, now: float) -> None:
    if not duty.get("kind"):
        return
    if now + 1e-6 < float(duty.get("until") or 0):
        return
    if not _at_sleep_home(car) and car.status != "at_depot":
        return
    duty["kind"] = ""
    duty["until"] = 0
    duty["stint_s"] = 0.0


def _counts_as_work(car: EmpireCar, duty: dict) -> bool:
    if duty.get("kind"):
        return False
    if car.status == "at_depot" and float(car.hold_until) - float(car.leg_started) >= BREAK_S - 1:
        return False
    return True


def _account_one_duty(car: EmpireCar, now: float, fleet_size: int) -> None:
    duty = _duty(car)
    if not duty.get("ready"):
        index = _sign_index(car.sign) or 1
        span = max(int(fleet_size), 1)
        duty["stint_s"] = float((index - 1) % span) * (STINT_S / float(span))
        duty["day_s"] = 0.0
        duty["week_s"] = 0.0
        duty["ready"] = 1
        duty["at"] = now
    _roll_duty_calendar(duty, now)
    _clear_finished_rest(car, duty, now)
    if not duty.get("kind"):
        last = float(duty.get("at") or 0)
        elapsed = 0.0
        if last > 0 and now >= last:
            elapsed = min(DUTY_TICK_CAP_S, now - last)
        if _counts_as_work(car, duty):
            duty["day_s"] = float(duty.get("day_s") or 0) + elapsed
            duty["week_s"] = float(duty.get("week_s") or 0) + elapsed
            duty["stint_s"] = float(duty.get("stint_s") or 0) + elapsed
    duty["at"] = now
    _save_duty(car, duty)


def _rest_reason(duty: dict) -> str:
    if float(duty.get("week_s") or 0) >= WORK_WEEK_S:
        return "week"
    if float(duty.get("day_s") or 0) >= WORK_DAY_S:
        return "day"
    if float(duty.get("stint_s") or 0) >= STINT_S:
        return "break"
    return ""


def _sit_at_home(car: EmpireCar, now: float) -> None:
    """Wait out the break at the door. The drive here has already counted."""
    duty = _duty(car)
    until = float(duty.get("until") or now)
    home = _stored_home(car) or car_position(car)
    car.passenger = ""
    car.fare_name = ""
    car.fare_lat = 0.0
    car.fare_lng = 0.0
    car.drop_lat = 0.0
    car.drop_lng = 0.0
    car.status = "at_depot"
    car.lat, car.lng = home
    car.route_json = json.dumps([[home[0], home[1]], [home[0], home[1]]])
    car.duration_s = 0.0
    car.leg_started = now
    car.hold_until = until
    car.osrm = True
    car.job = "on break"


def _send_home(car: EmpireCar, game: EmpireGame, now: float) -> None:
    """Drive to the sleep door. Time on this leg is the break, not work."""
    if _on_passenger_leg(car):
        return
    home = _stored_home(car)
    if home is None:
        return
    here = car_position(car)
    if haversine_km(here, home) <= 0.08:
        _sit_at_home(car, now)
        return
    end = _route_end(car)
    if car.status == "returning" and end is not None and haversine_km(end, home) <= ROAD_HAIL_KM:
        return
    _clear_empty(car)
    _begin(car, game, here, home, now, "returning")
    _store_job(car)


def _start_rest(car: EmpireCar, game: EmpireGame, now: float, kind: str) -> None:
    duty = _duty(car)
    if kind == "week":
        until = _next_london_monday(now)
    elif kind == "day":
        until = _next_london_midnight(now)
    else:
        until = now + BREAK_NEED_S
        kind = "break"
    duty["kind"] = kind
    duty["until"] = until
    _save_duty(car, duty)
    _release_uncollected(car)
    _send_home(car, game, now)


def _coverage_floor(count: int) -> int:
    """One taxi stays out of a pair. Two stay out when the fleet is larger. A lone taxi may sleep."""
    if count <= 1:
        return 0
    if count < 3:
        return 1
    return MIN_WORKING


def _apply_driver_hours(
    game: EmpireGame,
    now: float,
    cars: list[EmpireCar],
    draw_as: str | None,
) -> None:
    """Twelve hours a day, an hour off every four, sixty hours a week.

    The drive home counts as the break. At least two taxis stay out when the fleet has them.
    """
    taken: list[tuple[float, float]] = []
    for car in cars:
        _ensure_sleep_home(car, game, draw_as, taken)
        _account_one_duty(car, now, len(cars))
    owed: list[tuple[float, str, EmpireCar, str]] = []
    for car in cars:
        if _hours_resting(car) or _on_passenger_leg(car):
            continue
        duty = _duty(car)
        reason = _rest_reason(duty)
        if not reason:
            continue
        rank = float(duty.get("stint_s") or 0)
        if reason == "day":
            rank += 10_000_000
        elif reason == "week":
            rank += 20_000_000
        owed.append((rank, car.sign, car, reason))
    owed.sort(reverse=True)
    floor = _coverage_floor(len(cars))
    for _rank, _sign, car, reason in owed:
        working = [other for other in cars if not _hours_resting(other)]
        if len(working) - 1 < floor:
            break
        _start_rest(car, game, now, reason)
    for car in cars:
        if _hours_resting(car):
            _send_home(car, game, now)


def _travel_to_a_better_spot(
    game: EmpireGame,
    session: Session,
    now: float,
    draw_as: str | None = None,
) -> None:
    """Claimed taxis collect. Idle taxis drive toward a hail they can see.

    A loiter order waits until no hail is left. Two idle taxis in one fleet do not hunt the same hail.
    The claim itself still waits until the car is within ten minutes.
    When no hail is left, a taxi may drive to its patch, take a break, or move within two miles.
    The depot is for a break. 800000 is not a fare.
    Another firm passes its own draw seed. The player's seed stays the bare username.
    A bought car takes one fare inside the circle when its four-second purchase pause ends.
    """
    fleet_name = draw_as or game.username
    patch = loiter_for_fleet(game, draw_as)
    people = _people(session, _people_key(game.username))
    waiting = [person for person in people if not person.sign]
    claimed = {person.sign: person for person in people if person.sign}
    cars = _fleet_cars(session, fleet_name) if draw_as else _cars(session, game.username)
    _apply_driver_hours(game, now, cars, draw_as)
    free = sum(1 for car in cars if car.status == "at_depot" and car_may_depart(car))
    taken: list[tuple[float, float]] = [(float(person.lat), float(person.lng)) for person in people]
    for car in cars:
        if car.status == "at_depot":
            continue
        end = _route_end(car)
        if end is not None:
            taken.append(end)
    reserved: set[int] = set()
    pending: list[EmpireCar] = []
    for car in cars:
        try:
            if _fault is not None and _fault(car):
                raise RuntimeError("empire car fault")
            if _hours_resting(car) and not _on_passenger_leg(car):
                _send_home(car, game, now)
                continue
            hunting = car.status == "to_better_spot" and not car.passenger and not car.fare_name
            parked = car.status == "at_depot" and car_may_depart(car) and now >= float(car.hold_until)
            person = claimed.get(car.sign)
            if person is not None and not car.passenger and not car.fare_name:
                if hunting or parked:
                    _collect_claimed(car, game, person, now)
                continue
            if not hunting and not parked:
                continue
            if parked and _take_first_bought_fare(car, game, session, now, taken):
                continue
            depot = car_depot(car)
            start = car_position(car, depot)
            if patch is not None and not waiting:
                centre, radius = patch
                if _inside_loiter(start, centre, radius) and not _point_taken(start, tuple(taken)):
                    if hunting:
                        _stop_far_empty_leg(car, now)
                        _store_job(car)
                    elif float(car.hold_until) - float(car.leg_started) >= BREAK_S - 1:
                        car.leg_started = now
                        car.hold_until = now
                        _store_job(car)
                    taken.append(start)
                    continue
                if hunting:
                    end = _route_end(car)
                    if end is not None and haversine_km(end, centre) <= radius + ROAD_HAIL_KM:
                        taken.append(end)
                        continue
                spot = loiter_spot(fleet_name, car.sign, now, centre, radius, tuple(taken))
                if not _inside_loiter(spot, centre, radius) or _point_taken(spot, tuple(taken)):
                    continue
                if _inside_loiter(start, centre, radius) and haversine_km(start, spot) <= 0.05:
                    taken.append(start)
                    continue
                taken.append(spot)
                _clear_empty(car)
                _begin(car, game, start, spot, now, "to_better_spot")
                _store_job(car)
                continue
            if hunting:
                aimed = _open_hail_at(car, people)
                if aimed is not None:
                    reserved.add(int(aimed.id or 0))
                    continue
            pending.append(car)
        except Exception:
            log.exception("empire car %s %s failed", car.username, car.sign)
    waiting = [person for person in people if not person.sign]
    for car in pending:
        try:
            if _fault is not None and _fault(car):
                raise RuntimeError("empire car fault")
            if _hours_resting(car) and not _on_passenger_leg(car):
                _send_home(car, game, now)
                continue
            depot = car_depot(car)
            start = car_position(car, depot)
            hail = _nearest_open_hail(start, people, reserved)
            if hail is not None:
                reserved.add(int(hail.id or 0))
                _drive_toward_hail(car, game, hail, now)
                continue
            if waiting:
                if car.status == "to_better_spot":
                    _stop_far_empty_leg(car, now)
                    _store_job(car)
                continue
            if car_takes_a_break(fleet_name, car.sign, now, len(people), free):
                if haversine_km(start, depot) > 0.05:
                    _clear_empty(car)
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
            _clear_empty(car)
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
    """A new car stays at the depot until the purchase price is paid."""
    return int(paid_pence) >= NEW_CAR_PENCE


def car_may_depart(car: EmpireCar) -> bool:
    """The opening ten may leave. A bought car may leave only after it is paid for."""
    if car.sign in OPENING_SIGNS or 1 <= _sign_index(car.sign) <= 10:
        return True
    return car.depart_offset >= 0


def pay_new_car(car: EmpireCar, pence: int) -> bool:
    """Record the purchase. A smaller payment does not let the car leave."""
    if not new_car_may_leave(pence):
        return False
    if car.depart_offset < 0:
        car.depart_offset = 0
    return True


def buy_car(username: str, pence: int, now: float | None = None, session: Session | None = None) -> str | None:
    """Spend the purchase price to add T11 when this account's money covers it. A smaller payment adds nothing."""
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


def sell_car(username: str, sign: str, now: float | None = None, session: Session | None = None) -> bool:
    """Credit the sale price and remove an owned taxi.

    A passenger job stays. An unpaid car stays. The last two taxis stay.
    A hail claimed but not collected is released.
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
        cars = _cars(session, username)
        car = next((item for item in cars if item.sign == sign), None)
        if car is None or car_is_for_sale(car):
            return False
        if car.status in ("to_passenger", "with_passenger") or car.passenger or car.fare_name:
            return False
        owned = [item for item in cars if not car_is_for_sale(item)]
        if len(owned) <= 2:
            return False
        for person in _people(session, _people_key(username)):
            if person.sign == car.sign:
                person.sign = ""
        game.cash_pence = int(game.cash_pence or 0) + SELL_CAR_PENCE
        session.delete(car)
        session.commit()
        return True
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
    if pence == NOT_A_FARE_PENCE:
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
    if _hours_resting(car):
        destination = "home"
    elif len(route) >= 2:
        destination = place_name((float(route[-1][0]), float(route[-1][1])))
    else:
        destination = place_name((float(car.lat), float(car.lng)))
    trip = trip_fields(car)
    origin, dest = _car_doors(car)
    miles = _car_miles(car) + driven_miles(car, now)
    fare = _car_fare(car)
    driver = driver_share_pence(fare)
    row = {
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
        "origin": origin,
        "dest": dest,
        "trip_pence": trip["trip_pence"],
        "journey": (car.journey or "") if car.status == "with_passenger" else "",
        "for_sale": car_is_for_sale(car),
        "miles": round(miles, 4),
        "fare_in": fare,
        "fuel_pence": fuel_pence(miles),
        "driver_share": driver,
        "company_pence": fare - driver,
    }
    home = _stored_home(car)
    if home is not None:
        row["home"] = [home[0], home[1]]
    return row


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
            "sell_car": SELL_CAR_PENCE,
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
