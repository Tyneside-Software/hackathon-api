"""Account fleet. Game rows are never devices, and tests never open the live database."""
from __future__ import annotations

from datetime import datetime, timezone

import json
import math
import os
import random
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_fd, TEMP_DB = tempfile.mkstemp(prefix="empire-test-", suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + TEMP_DB.replace("\\", "/")
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

import app.empire as empire
from app.database import Base, SessionLocal
from app.models import Device, DeviceLink, EmpireCar, EmpireGame, LocationPing  # noqa: F401

empire.set_route_fn(lambda start, end: None)
NOW = 1_700_000_000
DAY = datetime(2023, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp()


def _ring(username: str) -> tuple[tuple[float, float], tuple[float, float]]:
    bearing = empire.trip_bearing(username)
    return (
        empire.destination(empire.DEPOT, bearing, 1.2),
        empire.destination(empire.DEPOT, bearing, 3.0),
    )


def _depot_bearing(point: tuple[float, float]) -> float:
    lat1 = math.radians(empire.DEPOT[0])
    lat2 = math.radians(point[0])
    dlng = math.radians(point[1] - empire.DEPOT[1])
    east = math.sin(dlng) * math.cos(lat2)
    north = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return math.degrees(math.atan2(east, north)) % 360


def _bearing_gap(left: float, right: float) -> float:
    gap = abs(left - right) % 360
    return min(gap, 360 - gap)


def _empty_patch() -> tuple[tuple[float, float], float]:
    """A small disk inside the circle that contains none of the published places."""
    radius = 0.45
    for bearing in range(0, 360, 15):
        for miles in (1.5, 2.5, 4.0, 6.0):
            centre = empire.destination(empire.DEPOT, bearing, miles)
            if not empire.inside_circle(centre):
                continue
            if empire.haversine_km(empire.CIRCLE_CENTRE, centre) + radius > empire.CIRCLE_RADIUS_KM:
                continue
            if any(
                empire.haversine_km(centre, (lat, lng)) <= radius
                for _name, lat, lng in empire.PLACES
            ):
                continue
            return centre, radius
    raise AssertionError("no empty patch")


def _quiet_account(prefix: str, stamp: float) -> str:
    for index in range(40):
        name = f"{prefix}{index}"
        if empire.spell_target(name, stamp) == 0:
            return name
    raise AssertionError(prefix)


def _draws_nearest_depot(account: str, stamp: float, count: int) -> list:
    draws = [empire.draw_journey(account, stamp, f"person-{index}") for index in range(count)]
    order = sorted(
        range(count),
        key=lambda item: (empire.haversine_km(empire.DEPOT, draws[item][1]), item),
    )
    return [draws[item] for item in order]


class EmpireTableTests(unittest.TestCase):
    def test_empire_tables_have_no_device_id(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        insp = inspect(engine)
        names = set(insp.get_table_names())
        self.assertIn("empire_games", names)
        self.assertIn("empire_cars", names)
        self.assertIn("empire_people", names)
        for table in ("empire_games", "empire_cars", "empire_people"):
            columns = {col["name"] for col in insp.get_columns(table)}
            self.assertNotIn("device_id", columns)
        device_columns = {col["name"] for col in insp.get_columns("devices")}
        self.assertIn("device_id", device_columns)
        self.assertIn("device_id", {col["name"] for col in insp.get_columns("location_pings")})
        self.assertIn("device_id", {col["name"] for col in insp.get_columns("device_links")})
        game_columns = {col["name"] for col in insp.get_columns("empire_games")}
        self.assertIn("cash_pence", game_columns)
        self.assertIn("loiter_firm", game_columns)
        self.assertIn("loiter_lat", game_columns)
        self.assertIn("loiter_lng", game_columns)
        self.assertIn("loiter_km", game_columns)
        self.assertIn("fare_in", game_columns)
        self.assertIn("miles_driven", game_columns)
        self.assertIn("firm_bookings", game_columns)
        car_columns = {col["name"] for col in insp.get_columns("empire_cars")}
        self.assertIn("job", car_columns)
        self.assertIn("firm", car_columns)
        self.assertIn("depot_lat", car_columns)
        self.assertIn("depot_lng", car_columns)
        person_columns = {col["name"] for col in insp.get_columns("empire_people")}
        self.assertIn("drop_lat", person_columns)
        self.assertIn("drop_lng", person_columns)
        self.assertIn("arrived", person_columns)

    def test_cash_pence_column_is_added_without_wiping_the_game(self) -> None:
        from app.database import ensure_empire_cash_column

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_games ("
                    "username VARCHAR(64) PRIMARY KEY, created_at VARCHAR(64), name_index INTEGER)"
                )
            )
            conn.execute(
                text("INSERT INTO empire_games (username, created_at, name_index) VALUES ('ada', '1', 0)")
            )
        ensure_empire_cash_column(engine)
        ensure_empire_cash_column(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_games")}
        with engine.connect() as conn:
            row = conn.execute(text("SELECT username, cash_pence FROM empire_games")).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_games")).scalar()
        self.assertIn("cash_pence", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.cash_pence, 0)
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_miles_column_is_added_without_wiping_the_game(self) -> None:
        from app.database import ensure_empire_miles_column

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_games ("
                    "username VARCHAR(64) PRIMARY KEY, created_at VARCHAR(64), "
                    "name_index INTEGER, cash_pence INTEGER, fare_in INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO empire_games (username, created_at, name_index, cash_pence, fare_in) "
                    "VALUES ('ada', '1', 0, 40, 1040)"
                )
            )
        ensure_empire_miles_column(engine)
        ensure_empire_miles_column(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_games")}
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT username, cash_pence, fare_in, miles_driven FROM empire_games")
            ).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_games")).scalar()
        self.assertIn("miles_driven", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.cash_pence, 40)
        self.assertEqual(row.fare_in, 1040)
        self.assertEqual(row.miles_driven, 0)
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_fare_in_column_is_added_without_wiping_the_game(self) -> None:
        from app.database import ensure_empire_fare_column

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_games ("
                    "username VARCHAR(64) PRIMARY KEY, created_at VARCHAR(64), "
                    "name_index INTEGER, cash_pence INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO empire_games (username, created_at, name_index, cash_pence) "
                    "VALUES ('ada', '1', 0, 40)"
                )
            )
        ensure_empire_fare_column(engine)
        ensure_empire_fare_column(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_games")}
        with engine.connect() as conn:
            row = conn.execute(text("SELECT username, cash_pence, fare_in FROM empire_games")).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_games")).scalar()
        self.assertIn("fare_in", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.cash_pence, 40)
        self.assertEqual(row.fare_in, 0)
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_loiter_columns_are_added_without_wiping_the_game(self) -> None:
        from app.database import ensure_empire_loiter_columns

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_games ("
                    "username VARCHAR(64) PRIMARY KEY, created_at VARCHAR(64), "
                    "name_index INTEGER, cash_pence INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO empire_games (username, created_at, name_index, cash_pence) "
                    "VALUES ('ada', '1', 0, 40)"
                )
            )
        ensure_empire_loiter_columns(engine)
        ensure_empire_loiter_columns(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_games")}
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT username, cash_pence, loiter_firm, loiter_lat, loiter_lng, loiter_km "
                    "FROM empire_games"
                )
            ).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_games")).scalar()
        self.assertIn("loiter_firm", columns)
        self.assertIn("loiter_lat", columns)
        self.assertIn("loiter_lng", columns)
        self.assertIn("loiter_km", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.cash_pence, 40)
        self.assertEqual(row.loiter_firm, "")
        self.assertEqual(row.loiter_lat, 0)
        self.assertEqual(row.loiter_lng, 0)
        self.assertEqual(row.loiter_km, 0)
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_bookings_column_is_added_without_wiping_the_game(self) -> None:
        from app.database import ensure_empire_bookings_column

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_games ("
                    "username VARCHAR(64) PRIMARY KEY, created_at VARCHAR(64), "
                    "name_index INTEGER, cash_pence INTEGER, fare_in INTEGER)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO empire_games (username, created_at, name_index, cash_pence, fare_in) "
                    "VALUES ('ada', '1', 0, 40, 1040)"
                )
            )
        ensure_empire_bookings_column(engine)
        ensure_empire_bookings_column(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_games")}
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT username, cash_pence, fare_in, firm_bookings FROM empire_games")
            ).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_games")).scalar()
        self.assertIn("firm_bookings", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.cash_pence, 40)
        self.assertEqual(row.fare_in, 1040)
        self.assertEqual(row.firm_bookings, "[]")
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_job_column_is_added_without_wiping_the_car(self) -> None:
        from app.database import ensure_empire_job_column

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_cars ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(64), sign VARCHAR(8), status VARCHAR(32))"
                )
            )
            conn.execute(
                text("INSERT INTO empire_cars (username, sign, status) VALUES ('ada', 'T1', 'to_passenger')")
            )
        ensure_empire_job_column(engine)
        ensure_empire_job_column(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_cars")}
        with engine.connect() as conn:
            row = conn.execute(text("SELECT username, sign, status, job FROM empire_cars")).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_cars")).scalar()
        self.assertIn("job", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.sign, "T1")
        self.assertEqual(row.status, "to_passenger")
        self.assertEqual(row.job, "")
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_firm_columns_are_added_without_wiping_the_car(self) -> None:
        from app.database import ensure_empire_firm_columns

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_cars ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(64), sign VARCHAR(8), status VARCHAR(32))"
                )
            )
            conn.execute(
                text("INSERT INTO empire_cars (username, sign, status) VALUES ('ada', 'T1', 'at_depot')")
            )
        ensure_empire_firm_columns(engine)
        ensure_empire_firm_columns(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_cars")}
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT username, sign, status, firm, depot_lat, depot_lng FROM empire_cars")
            ).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_cars")).scalar()
        self.assertIn("firm", columns)
        self.assertIn("depot_lat", columns)
        self.assertIn("depot_lng", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.sign, "T1")
        self.assertEqual(row.status, "at_depot")
        self.assertEqual(row.firm, "")
        self.assertEqual(row.depot_lat, 0)
        self.assertEqual(row.depot_lng, 0)
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_person_drop_columns_are_added_without_wiping_the_person(self) -> None:
        from app.database import ensure_empire_person_drop_columns

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_people ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(64), name VARCHAR(64), "
                    "lat FLOAT, lng FLOAT, sign VARCHAR(8))"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO empire_people (username, name, lat, lng, sign) "
                    "VALUES ('ada', 'Mina', 55.03, -1.51, '')"
                )
            )
        ensure_empire_person_drop_columns(engine)
        ensure_empire_person_drop_columns(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_people")}
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT username, name, sign, drop_lat, drop_lng FROM empire_people")
            ).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_people")).scalar()
        self.assertIn("drop_lat", columns)
        self.assertIn("drop_lng", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.name, "Mina")
        self.assertEqual(row.sign, "")
        self.assertEqual(row.drop_lat, 0)
        self.assertEqual(row.drop_lng, 0)
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_arrived_column_is_added_without_wiping_the_person(self) -> None:
        from app.database import ensure_empire_person_arrived_column

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE empire_people ("
                    "id INTEGER PRIMARY KEY, username VARCHAR(64), name VARCHAR(64), "
                    "lat FLOAT, lng FLOAT, sign VARCHAR(8))"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO empire_people (username, name, lat, lng, sign) "
                    "VALUES ('ada', 'Mina', 55.03, -1.51, '')"
                )
            )
        ensure_empire_person_arrived_column(engine)
        ensure_empire_person_arrived_column(engine)
        columns = {col["name"] for col in inspect(engine).get_columns("empire_people")}
        with engine.connect() as conn:
            row = conn.execute(text("SELECT username, name, sign, arrived FROM empire_people")).one()
            count = conn.execute(text("SELECT COUNT(*) FROM empire_people")).scalar()
        self.assertIn("arrived", columns)
        self.assertEqual(row.username, "ada")
        self.assertEqual(row.name, "Mina")
        self.assertEqual(row.sign, "")
        self.assertEqual(row.arrived, 0)
        self.assertEqual(count, 1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())


class EmpireRegisterTests(unittest.TestCase):
    def test_register_creates_three_cars_at_the_depot(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            created = client.post("/register", json={"username": "Clerk", "password": "password1"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["username"], "clerk")
        with SessionLocal() as session:
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "clerk")).all())
        signs = sorted((car.sign for car in cars), key=lambda sign: int(sign[1:]))
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        for car in cars:
            self.assertEqual(car.status, "at_depot")
            self.assertAlmostEqual(car.lat, 55.0367, places=4)
            self.assertAlmostEqual(car.lng, -1.5075, places=4)
        live = ROOT / "hackathon.db"
        self.assertNotEqual(Path(TEMP_DB).resolve(), live.resolve())

    def test_empire_feed_returns_ten_cars(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            created = client.post("/register", json={"username": "tenfeed", "password": "password1"})
            self.assertEqual(created.status_code, 201)
            token = client.post("/login", json={"username": "tenfeed", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual([car["sign"] for car in body["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_feed_returns_cash_pence_default_zero(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "cashzero", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "cashzero", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(body["cash_pence"], 0)
        self.assertIs(type(body["cash_pence"]), int)
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertEqual([car["sign"] for car in body["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_short_password_does_not_start_a_fleet(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.models import EmpireGame

        with TestClient(app) as client:
            short = client.post("/register", json={"username": "fleet7", "password": "1234567"})
            self.assertEqual(short.status_code, 422)
            ok = client.post("/register", json={"username": "fleet8", "password": "12345678"})
            self.assertEqual(ok.status_code, 201)
        with SessionLocal() as session:
            self.assertIsNone(session.get(EmpireGame, "fleet7"))
            self.assertIsNotNone(session.get(EmpireGame, "fleet8"))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_money_lines_are_for_one_account(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "moneya", "password": "password1"}).status_code, 201)
            self.assertEqual(client.post("/register", json={"username": "moneyb", "password": "password1"}).status_code, 201)
            with SessionLocal() as session:
                session.add(
                    EmpireCar(
                        username="moneyb",
                        sign="T11",
                        colour="#ffffff",
                        status="at_depot",
                        passenger="",
                        lat=55.0367,
                        lng=-1.5075,
                        route_json="[]",
                        duration_s=0,
                        leg_started=0,
                        osrm=False,
                        trail_json="[]",
                        fare_name="",
                        hold_until=10**12,
                        next_check=10**12,
                        depart_offset=-1,
                    )
                )
                session.commit()
            token_a = client.post("/login", json={"username": "moneya", "password": "password1"}).json()["access_token"]
            token_b = client.post("/login", json={"username": "moneyb", "password": "password1"}).json()["access_token"]
            ada = client.get("/v1/empire", headers={"Authorization": f"Bearer {token_a}"}).json()
            bee = client.get("/v1/empire", headers={"Authorization": f"Bearer {token_b}"}).json()
        self.assertEqual(ada["username"], "moneya")
        self.assertEqual(bee["username"], "moneyb")
        self.assertEqual(ada["standing"], 1500 * 10)
        self.assertEqual(bee["standing"], 1500 * 11)
        self.assertEqual(ada["fare_in"], 0)
        self.assertEqual(ada["driver_share"], 0)
        self.assertEqual(ada["fuel"], 0)
        self.assertNotIn("T11", [car["sign"] for car in ada["cars"]])
        self.assertIn("T11", [car["sign"] for car in bee["cars"]])
        self.assertNotIn(16500, [ada["fare_in"], ada["driver_share"], ada["fuel"], ada["standing"], ada["new_car"]])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_ten_car_feed_names_check_s_and_trail_s(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "clockten", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "clockten", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual([car["sign"] for car in body["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertEqual(body["check_s"], 10)
        self.assertEqual(body["trail_s"], 600)
        self.assertIs(type(body["check_s"]), int)
        self.assertIs(type(body["trail_s"]), int)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_ten_car_feed_names_server_now(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "nowten", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "nowten", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual([car["sign"] for car in body["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertIs(type(body["server_now"]), int)
        self.assertGreater(body["server_now"], 1_700_000_000)
        self.assertLess(body["server_now"], 2_000_000_000)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_ten_car_feed_grows_by_the_bought_car(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "bought1", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "bought1", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            before = client.get("/v1/empire", headers=headers).json()
            with SessionLocal() as session:
                added = empire.buy_car("bought1", 800000, session=session)
            after = client.get("/v1/empire", headers=headers).json()
        self.assertEqual([car["sign"] for car in before["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertEqual(added, "T11")
        self.assertEqual([car["sign"] for car in after["cars"]], [f"T{index}" for index in range(1, 12)])
        self.assertEqual(before["new_car"], 800000)
        self.assertEqual(after["new_car"], 800000)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_post_buy_spends_800000_and_adds_t11(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "routebuy", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "routebuy", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            before = client.get("/v1/empire", headers=headers)
            short = client.post("/v1/empire/buy", headers=headers, json={"pence": 799999})
            after_short = client.get("/v1/empire", headers=headers)
            bought = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            again = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            after = client.get("/v1/empire", headers=headers)
        self.assertEqual(before.status_code, 200)
        self.assertEqual([car["sign"] for car in before.json()["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertGreaterEqual(short.status_code, 400)
        self.assertNotIn("T11", [car["sign"] for car in after_short.json()["cars"]])
        self.assertEqual([car["sign"] for car in after_short.json()["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertEqual(bought.status_code, 200)
        self.assertEqual(bought.json()["sign"], "T11")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["sign"], "T11")
        signs = [car["sign"] for car in after.json()["cars"]]
        self.assertEqual(signs, [f"T{index}" for index in range(1, 12)])
        self.assertNotIn("T12", signs)
        self.assertEqual(before.json()["fare_in"], 0)
        self.assertEqual(after.json()["fare_in"], 0)
        self.assertEqual(before.json()["new_car"], 800000)
        self.assertEqual(after.json()["new_car"], 800000)
        self.assertEqual(empire.NEW_CAR_PENCE, 800000)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_post_buy_subtracts_800000_from_that_account_only(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "cashpost", "password": "password1"}).status_code, 201)
            self.assertEqual(client.post("/register", json={"username": "cashstay", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "cashpost", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            other_token = client.post("/login", json={"username": "cashstay", "password": "password1"}).json()["access_token"]
            other_headers = {"Authorization": f"Bearer {other_token}"}
            before = client.get("/v1/empire", headers=headers).json()
            other_before = client.get("/v1/empire", headers=other_headers).json()
            with SessionLocal() as session:
                game = session.get(EmpireGame, "cashpost")
                game.cash_pence = 500000
                game.fare_in = 1040
                session.commit()
            short = client.post("/v1/empire/buy", headers=headers, json={"pence": 799999})
            after_short = client.get("/v1/empire", headers=headers).json()
            bought = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            again = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            after = client.get("/v1/empire", headers=headers).json()
            other = client.get("/v1/empire", headers=other_headers).json()
        self.assertEqual(before["cash_pence"], 0)
        self.assertEqual(before["fare_in"], 0)
        self.assertEqual(other_before["cash_pence"], 0)
        self.assertGreaterEqual(short.status_code, 400)
        self.assertEqual(after_short["cash_pence"], 500000)
        self.assertEqual(after_short["fare_in"], 1040)
        self.assertNotIn("T11", [car["sign"] for car in after_short["cars"]])
        self.assertEqual(bought.status_code, 200)
        self.assertEqual(bought.json()["sign"], "T11")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["sign"], "T11")
        self.assertEqual(after["cash_pence"], 500000 - 800000)
        self.assertEqual(after["fare_in"], 1040)
        self.assertNotEqual(after["fare_in"], 1040 - 800000)
        self.assertNotEqual(after["cash_pence"], 800000)
        self.assertEqual(after["new_car"], 800000)
        self.assertEqual([car["sign"] for car in after["cars"]], [f"T{index}" for index in range(1, 12)])
        self.assertNotIn("T12", [car["sign"] for car in after["cars"]])
        self.assertEqual(other["cash_pence"], 0)
        self.assertEqual(other["fare_in"], 0)
        self.assertEqual(len(other["cars"]), 10)
        self.assertNotIn(2860, (after["cash_pence"], after["fare_in"], other["cash_pence"]))
        self.assertNotIn(1130, (after["cash_pence"], after["fare_in"], other["cash_pence"]))
        self.assertNotIn(1240, (after["cash_pence"], after["fare_in"], other["cash_pence"]))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_post_cancel_before_the_car_moves_adds_no_fare_and_no_fuel(self) -> None:
        import time

        from fastapi.testclient import TestClient

        from app.main import app

        shire = empire.place_point("Shiremoor")
        cobalt = empire.place_point("Cobalt")
        with TestClient(app) as client:
            self.assertEqual(client.post("/v1/empire/cancel", json={"sign": "T1"}).status_code, 401)
            self.assertEqual(
                client.post("/register", json={"username": "stillpost", "password": "password1"}).status_code,
                201,
            )
            token = client.post("/login", json={"username": "stillpost", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            self.assertEqual(client.get("/v1/empire", headers=headers).status_code, 200)
            started = time.time()
            with SessionLocal() as session:
                game = session.get(EmpireGame, "stillpost")
                game.fare_in = 1040
                game.miles_driven = 3
                game.cash_pence = 40
                car = session.scalars(
                    select(EmpireCar).where(EmpireCar.username == "stillpost", EmpireCar.sign == "T1")
                ).one()
                car.status = "to_passenger"
                car.passenger = "Mina"
                car.fare_name = "Mina"
                car.lat, car.lng = shire
                car.fare_lat, car.fare_lng = shire
                car.drop_lat, car.drop_lng = cobalt
                car.route_json = json.dumps([[shire[0], shire[1]], [cobalt[0], cobalt[1]]])
                car.duration_s = 100000
                car.leg_started = started
                session.commit()
            cancelled = client.post("/v1/empire/cancel", headers=headers, json={"sign": "T1"})
            body = cancelled.json()
            again = client.post("/v1/empire/cancel", headers=headers, json={"sign": "T1"})
            missing = client.post("/v1/empire/cancel", headers=headers, json={"sign": "T99"})
            later = client.get("/v1/empire", headers=headers).json()
            with SessionLocal() as session:
                game = session.get(EmpireGame, "stillpost")
                stored_miles = game.miles_driven
                stored_fare = game.fare_in
                stored_cash = game.cash_pence
        parked = next(car for car in body["cars"] if car["sign"] == "T1")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(body["fare_in"], 1040)
        self.assertEqual(body["cash_pence"], 40)
        self.assertEqual(body["driver_share"], 520)
        self.assertEqual(stored_miles, 3)
        self.assertEqual(stored_fare, 1040)
        self.assertEqual(stored_cash, 40)
        self.assertEqual(parked["status"], "at_depot")
        self.assertEqual(parked["job"], "waiting")
        self.assertEqual(parked["passenger"], "")
        self.assertEqual(parked["trip_pence"], 0)
        self.assertEqual(parked["pickup"], "")
        self.assertEqual(parked["drop"], "")
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(again.status_code, 400)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(later["fare_in"], 1040)
        self.assertEqual(later["cash_pence"], 40)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertNotEqual(body["fuel"], 800000)
        self.assertNotIn(2860, (body["fare_in"], body["fuel"], body["cash_pence"], parked["trip_pence"]))
        self.assertNotIn(1130, (body["fare_in"], body["fuel"], body["cash_pence"]))
        self.assertNotIn(1240, (body["fare_in"], body["fuel"], body["cash_pence"]))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_post_cancel_after_the_car_has_left_adds_fuel_and_no_fare(self) -> None:
        import time

        from fastapi.testclient import TestClient

        from app.main import app

        shire = empire.place_point("Shiremoor")
        cobalt = empire.place_point("Cobalt")
        route = [[shire[0], shire[1]], [cobalt[0], cobalt[1]]]
        full = empire.route_miles(route)
        with TestClient(app) as client:
            self.assertEqual(
                client.post("/register", json={"username": "leftpost", "password": "password1"}).status_code,
                201,
            )
            token = client.post("/login", json={"username": "leftpost", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            self.assertEqual(client.get("/v1/empire", headers=headers).status_code, 200)
            with SessionLocal() as session:
                game = session.get(EmpireGame, "leftpost")
                game.fare_in = 1040
                game.miles_driven = 3
                game.cash_pence = 40
                car = session.scalars(
                    select(EmpireCar).where(EmpireCar.username == "leftpost", EmpireCar.sign == "T1")
                ).one()
                car.status = "to_passenger"
                car.passenger = "Mina"
                car.fare_name = "Mina"
                car.lat, car.lng = shire
                car.fare_lat, car.fare_lng = shire
                car.drop_lat, car.drop_lng = cobalt
                car.route_json = json.dumps(route)
                car.duration_s = 360
                car.leg_started = time.time() - 180
                session.commit()
            cancelled = client.post("/v1/empire/cancel", headers=headers, json={"sign": "T1"})
            body = cancelled.json()
            with SessionLocal() as session:
                game = session.get(EmpireGame, "leftpost")
                stored_miles = game.miles_driven
                stored_fare = game.fare_in
                stored_cash = game.cash_pence
            again = client.post("/v1/empire/cancel", headers=headers, json={"sign": "T1"})
            with SessionLocal() as session:
                again_miles = session.get(EmpireGame, "leftpost").miles_driven
        parked = next(car for car in body["cars"] if car["sign"] == "T1")
        self.assertEqual(cancelled.status_code, 200)
        self.assertGreater(stored_miles, 3)
        self.assertLess(stored_miles, 3 + full)
        self.assertAlmostEqual(stored_miles, 3 + full * 0.5, delta=full * 0.05)
        self.assertEqual(stored_fare, 1040)
        self.assertEqual(stored_cash, 40)
        self.assertEqual(body["fare_in"], 1040)
        self.assertEqual(body["cash_pence"], 40)
        self.assertEqual(body["driver_share"], 520)
        self.assertEqual(parked["status"], "at_depot")
        self.assertEqual(parked["passenger"], "")
        self.assertEqual(parked["trip_pence"], 0)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(again.status_code, 400)
        self.assertAlmostEqual(again_miles, stored_miles, places=5)
        self.assertNotEqual(body["fare_in"], 1040 + empire.north_tyneside_tariff_pence(full, 360))
        self.assertNotEqual(body["fuel"], 800000)
        self.assertNotIn(2860, (body["fare_in"], body["fuel"], body["cash_pence"]))
        self.assertNotIn(1130, (body["fare_in"], body["fuel"], body["cash_pence"]))
        self.assertNotIn(1240, (body["fare_in"], body["fuel"], body["cash_pence"]))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_post_buy_without_a_token_adds_no_car(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "notoken1", "password": "password1"}).status_code, 201)
            missing = client.post("/v1/empire/buy", json={"pence": 800000})
            token = client.post("/login", json={"username": "notoken1", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            before = client.get("/v1/empire", headers=headers)
            bought = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            after = client.get("/v1/empire", headers=headers)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual([car["sign"] for car in before.json()["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", [car["sign"] for car in before.json()["cars"]])
        self.assertEqual(bought.status_code, 200)
        self.assertEqual(bought.json()["sign"], "T11")
        self.assertEqual([car["sign"] for car in after.json()["cars"]], [f"T{index}" for index in range(1, 12)])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_post_buy_uses_the_token_account(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "tokacct", "password": "password1"}).status_code, 201)
            self.assertEqual(client.post("/register", json={"username": "bodyacct", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "tokacct", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            bought = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000, "username": "bodyacct"})
            owner = client.get("/v1/empire", headers=headers)
            other_token = client.post("/login", json={"username": "bodyacct", "password": "password1"}).json()["access_token"]
            other = client.get("/v1/empire", headers={"Authorization": f"Bearer {other_token}"})
        self.assertEqual(bought.status_code, 200)
        self.assertEqual(bought.json()["sign"], "T11")
        self.assertEqual([car["sign"] for car in owner.json()["cars"]], [f"T{index}" for index in range(1, 12)])
        self.assertEqual([car["sign"] for car in other.json()["cars"]], [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", [car["sign"] for car in other.json()["cars"]])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_get_after_buy_lists_t11_and_the_ten(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "listafter", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "listafter", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            bought = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            body = client.get("/v1/empire", headers=headers)
        signs = [car["sign"] for car in body.json()["cars"]]
        self.assertEqual(bought.status_code, 200)
        self.assertEqual(bought.json()["sign"], "T11")
        self.assertEqual(body.status_code, 200)
        self.assertEqual(signs[:10], [f"T{index}" for index in range(1, 11)])
        self.assertEqual(signs, [f"T{index}" for index in range(1, 12)])
        self.assertNotIn("T12", signs)
        self.assertEqual(body.json()["new_car"], 800000)
        self.assertEqual(body.json()["fare_in"], 0)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_one_purchase_spends_800000_once(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "spendonce", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "spendonce", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            first = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            second = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            body = client.get("/v1/empire", headers=headers).json()
        signs = [car["sign"] for car in body["cars"]]
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["sign"], "T11")
        self.assertEqual(second.json()["sign"], "T11")
        self.assertEqual(signs.count("T11"), 1)
        self.assertEqual(signs, [f"T{index}" for index in range(1, 12)])
        self.assertNotIn("T12", signs)
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertNotEqual(body["fare_in"], 1_600_000)
        self.assertEqual(body["new_car"], 800000)
        self.assertEqual(empire.NEW_CAR_PENCE, 800000)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_payment_under_800000_is_not_a_successful_purchase(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "underpay", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "underpay", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            short = client.post("/v1/empire/buy", headers=headers, json={"pence": 799999})
            after_short = client.get("/v1/empire", headers=headers).json()
            bought = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            after = client.get("/v1/empire", headers=headers).json()
        short_signs = [car["sign"] for car in after_short["cars"]]
        signs = [car["sign"] for car in after["cars"]]
        self.assertGreaterEqual(short.status_code, 400)
        self.assertLess(bought.status_code, 300)
        self.assertGreaterEqual(bought.status_code, 200)
        self.assertEqual(bought.json()["sign"], "T11")
        self.assertEqual(short_signs, [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", short_signs)
        self.assertEqual(signs[:10], [f"T{index}" for index in range(1, 11)])
        self.assertIn("T11", signs)
        self.assertEqual(empire.NEW_CAR_PENCE, 800000)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_purchase_body_with_no_pence_adds_no_car(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "nopence", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "nopence", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            missing = client.post("/v1/empire/buy", headers=headers, json={})
            after_missing = client.get("/v1/empire", headers=headers).json()
            bought = client.post("/v1/empire/buy", headers=headers, json={"pence": 800000})
            after = client.get("/v1/empire", headers=headers).json()
        missing_signs = [car["sign"] for car in after_missing["cars"]]
        signs = [car["sign"] for car in after["cars"]]
        self.assertGreaterEqual(missing.status_code, 400)
        self.assertEqual(missing_signs, [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", missing_signs)
        self.assertGreaterEqual(bought.status_code, 200)
        self.assertLess(bought.status_code, 300)
        self.assertEqual(bought.json()["sign"], "T11")
        self.assertEqual(signs[:10], [f"T{index}" for index in range(1, 11)])
        self.assertIn("T11", signs)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_rejected_bearer_token_on_the_purchase_adds_no_car(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "badtoken", "password": "password1"}).status_code, 201)
            rejected = client.post(
                "/v1/empire/buy",
                headers={"Authorization": "Bearer not-a-real-token"},
                json={"pence": 800000},
            )
            token = client.post("/login", json={"username": "badtoken", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        signs = [car["sign"] for car in body["cars"]]
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", signs)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_purchase_without_a_valid_token_answers_401(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "auth401", "password": "password1"}).status_code, 201)
            missing = client.post("/v1/empire/buy", json={"pence": 800000})
            rejected = client.post(
                "/v1/empire/buy",
                headers={"Authorization": "Bearer not-a-real-token"},
                json={"pence": 800000},
            )
            token = client.post("/login", json={"username": "auth401", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        signs = [car["sign"] for car in body["cars"]]
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", signs)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_company_figure_minus_1120_stays_off_the_ten_car_feed(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        def integers(value):
            if type(value) is int:
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from integers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from integers(item)

        with TestClient(app) as client:
            self.assertEqual(client.post("/register", json={"username": "off1120", "password": "password1"}).status_code, 201)
            token = client.post("/login", json={"username": "off1120", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(len(body["cars"]), 10)
        self.assertNotIn(-1120, list(integers(body)))
        self.assertNotIn("-1120", str(body))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())


class EmpireFleetTests(unittest.TestCase):
    def setUp(self) -> None:
        empire.set_route_fn(lambda start, end: None)
        empire.set_fault(None)
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        empire.set_route_fn(lambda start, end: None)
        empire.set_fault(None)

    def _car(self, body: dict, sign: str) -> dict:
        return next(car for car in body["cars"] if car["sign"] == sign)

    def test_fleet_starts_with_ten_cars_at_the_depot(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ten", NOW, session)
            body = empire.snapshot("ten", NOW, session)
        self.assertEqual([car["sign"] for car in body["cars"]], [f"T{index}" for index in range(1, 11)])
        for car in body["cars"]:
            self.assertEqual(car["status"], "at_depot")
            self.assertAlmostEqual(car["lat"], 55.0367, places=4)
            self.assertAlmostEqual(car["lng"], -1.5075, places=4)

    def test_ten_car_feed_does_not_grow_an_eleventh_sign(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.ensure_game("ada", NOW + 30, session)
            empire.tick_all(NOW + 64, session)
            body = empire.snapshot("ada", NOW + 64, session)
            stored = [car.sign for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all()]
        signs = [car["sign"] for car in body["cars"]]
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        self.assertEqual(sorted(stored, key=lambda sign: int(sign[1:])), signs)
        self.assertNotIn("T11", signs)
        self.assertEqual(len(stored), 10)

    def test_paid_800000_adds_one_car_and_keeps_the_ten(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            added = empire.buy_car("ada", 800000, NOW, session)
            body = empire.snapshot("ada", NOW, session)
            stored = [car.sign for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all()]
        signs = [car["sign"] for car in body["cars"]]
        self.assertEqual(added, "T11")
        self.assertEqual(signs[:10], [f"T{index}" for index in range(1, 11)])
        self.assertEqual(signs, [f"T{index}" for index in range(1, 12)])
        self.assertEqual(sorted(stored, key=lambda sign: int(sign[1:])), signs)
        self.assertEqual(len(stored), 11)
        self.assertNotIn("T12", signs)

    def test_payment_under_800000_does_not_add_a_car(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            added = empire.buy_car("ada", 799999, NOW, session)
            empire.tick_all(NOW + 64, session)
            body = empire.snapshot("ada", NOW + 64, session)
        self.assertIsNone(added)
        signs = [car["sign"] for car in body["cars"]]
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", signs)
        self.assertEqual(empire.NEW_CAR_PENCE, 800000)
        self.assertEqual(body["new_car"], 800000)
        for car in body["cars"]:
            self.assertNotEqual(car["sign"], "T11")

    def test_buying_a_car_leaves_fuel_and_standing_unchanged(self) -> None:
        fuel_rate = empire.FUEL_PENCE_PER_MILE
        standing_rate = empire.STANDING_PENCE
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            before = empire.snapshot("ada", NOW, session)
            added = empire.buy_car("ada", 800000, NOW, session)
            after = empire.snapshot("ada", NOW, session)
        self.assertEqual(added, "T11")
        self.assertEqual(len(before["cars"]), 10)
        self.assertEqual(len(after["cars"]), 11)
        self.assertEqual(fuel_rate, 20)
        self.assertEqual(standing_rate, 1500)
        self.assertEqual(empire.FUEL_PENCE_PER_MILE, fuel_rate)
        self.assertEqual(empire.STANDING_PENCE, standing_rate)
        self.assertEqual(before["fuel"], 0)
        self.assertEqual(after["fuel"], before["fuel"])
        self.assertEqual(empire.fuel_pence(7), 140)
        self.assertEqual(before["standing"], standing_rate * 10)
        self.assertEqual(after["standing"], standing_rate * len(after["cars"]))
        self.assertNotEqual(after["fuel"], 800000)
        self.assertNotEqual(after["standing"], before["standing"] + 800000)
        self.assertEqual(after["fare_in"], before["fare_in"])
        self.assertEqual(after["new_car"], 800000)

    def test_bought_car_is_not_a_device(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            before_devices = session.scalar(select(func.count()).select_from(Device))
            added = empire.buy_car("ada", 800000, NOW, session)
            devices = session.scalar(select(func.count()).select_from(Device))
            links = session.scalar(select(func.count()).select_from(DeviceLink))
            pings = session.scalar(select(func.count()).select_from(LocationPing))
            bought = session.scalar(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T11"))
        self.assertEqual(added, "T11")
        self.assertEqual(before_devices, 0)
        self.assertEqual(devices, 0)
        self.assertEqual(links, 0)
        self.assertEqual(pings, 0)
        self.assertIsNotNone(bought)
        self.assertEqual(bought.sign, "T11")
        self.assertNotIn("device_id", EmpireCar.__table__.columns.keys())
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_purchase_adds_a_car_only_to_that_account(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.ensure_game("bee", NOW, session)
            added = empire.buy_car("ada", 800000, NOW, session)
            ada_stored = [car.sign for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all()]
            bee_stored = [car.sign for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "bee")).all()]
            ada = empire.snapshot("ada", NOW, session)
            bee = empire.snapshot("bee", NOW, session)
        ada_signs = [car["sign"] for car in ada["cars"]]
        bee_signs = [car["sign"] for car in bee["cars"]]
        self.assertEqual(added, "T11")
        self.assertEqual(ada_signs, [f"T{index}" for index in range(1, 12)])
        self.assertEqual(sorted(ada_stored, key=lambda sign: int(sign[1:])), ada_signs)
        self.assertEqual(bee_signs, [f"T{index}" for index in range(1, 11)])
        self.assertEqual(sorted(bee_stored, key=lambda sign: int(sign[1:])), bee_signs)
        self.assertNotIn("T11", bee_signs)
        self.assertEqual(len(bee_stored), 10)

    def test_bought_car_starts_at_the_shiremoor_depot(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            added = empire.buy_car("ada", 800000, NOW, session)
            body = empire.snapshot("ada", NOW, session)
            stored = session.scalar(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T11"))
        car = next(item for item in body["cars"] if item["sign"] == "T11")
        self.assertEqual(added, "T11")
        self.assertEqual(empire.DEPOT, (55.0367, -1.5075))
        self.assertEqual(car["status"], "at_depot")
        self.assertAlmostEqual(car["lat"], 55.0367, places=4)
        self.assertAlmostEqual(car["lng"], -1.5075, places=4)
        self.assertIsNotNone(stored)
        self.assertAlmostEqual(stored.lat, 55.0367, places=4)
        self.assertAlmostEqual(stored.lng, -1.5075, places=4)
        self.assertEqual(stored.status, "at_depot")

    def test_tick_does_not_spend_800000(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            before = empire.snapshot("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            body = empire.snapshot("ada", NOW + 64, session)
        before_signs = [car["sign"] for car in before["cars"]]
        signs = [car["sign"] for car in body["cars"]]
        self.assertEqual(before_signs, [f"T{index}" for index in range(1, 11)])
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        self.assertNotIn("T11", signs)
        self.assertEqual(before["fare_in"], 0)
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], before["fare_in"] - 800000)
        self.assertEqual(body["standing"], empire.STANDING_PENCE * 10)
        self.assertNotEqual(body["standing"], before["standing"] - 800000)
        self.assertEqual(body["new_car"], 800000)
        self.assertEqual(empire.NEW_CAR_PENCE, 800000)

    def test_bought_car_first_fare_stays_inside_the_circle(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            added = empire.buy_car("ada", 800000, NOW, session)
            empire.tick_all(NOW + 1, session)
            car = session.scalar(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T11"))
            stored = [row.sign for row in session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all()]
            claimed = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T1")
            ).one()
            pickup = (claimed.fare_lat, claimed.fare_lng)
            drop = (claimed.drop_lat, claimed.drop_lng)
            claimed_status = claimed.status
        self.assertEqual(added, "T11")
        self.assertIsNotNone(car)
        places = {(lat, lng) for _name, lat, lng in empire.PLACES}
        self.assertEqual(claimed_status, "to_passenger")
        self.assertIn(pickup, places)
        self.assertIn(drop, places)
        self.assertNotEqual(pickup, drop)
        self.assertTrue(empire.inside_circle(pickup))
        self.assertTrue(empire.inside_circle(drop))
        self.assertNotEqual(car.status, "to_passenger")
        self.assertEqual(car.passenger, "")
        self.assertEqual((car.fare_lat, car.fare_lng, car.drop_lat, car.drop_lng), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(sorted(stored, key=lambda sign: int(sign[1:])), [f"T{index}" for index in range(1, 12)])
        self.assertNotIn("T12", stored)

    def test_tick_advances_every_account_without_a_browser(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.ensure_game("bee", NOW, session)
            empire.tick_all(NOW + 1, session)
            ada = empire.snapshot("ada", NOW + 1, session)
            bee = empire.snapshot("bee", NOW + 1, session)
        self.assertEqual(self._car(ada, "T1")["status"], "to_passenger")
        self.assertEqual(self._car(bee, "T1")["status"], "to_passenger")
        self.assertFalse(empire.clock_running())

    def test_check_in_waits_until_10_seconds(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 9, session)
            early = empire.snapshot("ada", NOW + 9, session)
            empire.tick_all(NOW + 10, session)
            late = empire.snapshot("ada", NOW + 10, session)
        self.assertEqual(self._car(early, "T1")["trail"], [])
        trail = self._car(late, "T1")["trail"]
        self.assertEqual(len(trail), 1)
        self.assertEqual(len(trail[0]), 3)
        self.assertEqual(trail[0][2], NOW + 10)

    def test_missing_osrm_is_a_straight_line_at_30_mph(self) -> None:
        calls = []

        def offline(start, end):
            calls.append((start, end))
            return None

        empire.set_route_fn(offline)
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
        car = self._car(body, "T1")
        self.assertTrue(calls)
        self.assertFalse(car["osrm"])
        self.assertEqual(len(car["route"]), 2)
        self.assertFalse(body["osrm_unavailable"] is False and car["status"] != "to_passenger")
        self.assertTrue(body["osrm_unavailable"])

    def test_missed_osrm_leg_is_30_mph_for_each_of_the_ten(self) -> None:
        empire.set_route_fn(lambda start, end: None)
        self.assertEqual(empire.MPH, 30.0)
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            empire.tick_all(DAY + 64, session)
            body = empire.snapshot("ada", DAY + 64, session)
        self.assertTrue(body["osrm_unavailable"])
        self.assertEqual(len(body["cars"]), 10)
        moving = [car for car in body["cars"] if car["status"] != "at_depot"]
        self.assertTrue(moving)
        self.assertTrue(any(car["status"] == "to_passenger" for car in moving))
        for car in moving:
            self.assertFalse(car["osrm"])
            self.assertGreaterEqual(len(car["route"]), 2)
            start = (car["route"][0][0], car["route"][0][1])
            end = (car["route"][-1][0], car["route"][-1][1])
            miles = empire.haversine_km(start, end) / empire.KM_PER_MILE
            if miles < 0.001:
                miles = 0.001
            self.assertAlmostEqual(car["duration_s"], miles / 30.0 * 3600.0, places=3)

    def test_drop_off_returns_to_the_depot_then_waits_four_seconds(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
            self.assertEqual(self._car(body, "T1")["status"], "to_passenger")
            self.assertTrue(body["fares"])
            collecting = self._car(body, "T1")
            arrived = collecting["leg_started"] + collecting["duration_s"]
            empire.tick_all(arrived + 1, session)
            body = empire.snapshot("ada", arrived + 1, session)
            self.assertEqual(self._car(body, "T1")["status"], "with_passenger")
            self.assertFalse([fare for fare in body["fares"] if fare["sign"] == "T1"])
            carrying = self._car(body, "T1")
            back = carrying["leg_started"] + carrying["duration_s"]
            drop_at = (carrying["route"][-1][0], carrying["route"][-1][1])
            earned = empire.north_tyneside_tariff_pence(
                empire.route_miles(carrying["route"]), carrying["duration_s"]
            )
            empire.tick_all(back + 1, session)
            body = empire.snapshot("ada", back + 1, session)
            finished = self._car(body, "T1")
            self.assertNotEqual(finished["status"], "with_passenger")
            self.assertEqual(len(body["cars"]), 10)
            self.assertEqual(body["fare_in"], earned)
            self.assertNotEqual(body["fare_in"], 0)
            self.assertNotEqual(body["fare_in"], 800000)
            self.assertEqual(body["driver_share"], earned // 2)
            if finished["status"] != "at_depot":
                self.assertGreaterEqual(len(finished["route"]), 2)
                self.assertAlmostEqual(finished["route"][0][0], drop_at[0], places=4)
                self.assertAlmostEqual(finished["route"][0][1], drop_at[1], places=4)

    def test_each_of_the_ten_waits_four_seconds(self) -> None:
        self.assertEqual(empire.DEPOT_PAUSE_S, 4)
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for car in cars:
                car.status = "returning"
                car.route_json = "[[55.0367, -1.5075], [55.0367, -1.5075]]"
                car.duration_s = 10
                car.leg_started = DAY
                car.hold_until = DAY
            session.commit()
            empire.tick_all(DAY + 10, session)
            arrived = empire.snapshot("ada", DAY + 10, session)
            empire.tick_all(DAY + 13, session)
            waiting = empire.snapshot("ada", DAY + 13, session)
            empire.tick_all(DAY + 14, session)
            left = empire.snapshot("ada", DAY + 14, session)
        for index in range(1, 11):
            sign = f"T{index}"
            self.assertEqual(self._car(arrived, sign)["status"], "at_depot")
            self.assertEqual(self._car(waiting, sign)["status"], "at_depot")
            if self._car(arrived, sign)["job"] == "on break":
                self.assertEqual(self._car(left, sign)["status"], "at_depot")
                self.assertEqual(self._car(left, sign)["job"], "on break")
            else:
                self.assertNotEqual(self._car(left, sign)["status"], "at_depot")

    def test_feed_is_one_account_and_needs_a_token(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/empire").status_code, 401)
            self.assertEqual(client.post("/register", json={"username": "ada", "password": "password1"}).status_code, 201)
            other = client.post("/register", json={"username": "bee", "password": "password1"})
            self.assertEqual(other.status_code, 201)
            token = client.post("/login", json={"username": "ada", "password": "password1"}).json()["access_token"]
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(body["username"], "ada")
        self.assertEqual(len(body["cars"]), 10)
        self.assertNotIn("bee", {car.get("passenger") for car in body["cars"]})

    def test_two_feeds_do_not_share_fare_coordinates(self) -> None:
        left = right = None
        for i in range(80):
            a = f"left{i}"
            b = f"right{i}"
            if empire.night_ends(a)[0] != empire.night_ends(b)[0]:
                left, right = a, b
                break
        self.assertIsNotNone(left)
        with self.Session() as session:
            empire.ensure_game(left, NOW, session)
            empire.ensure_game(right, NOW, session)
            empire.tick_all(NOW + 1, session)
            one = empire.snapshot(left, NOW + 1, session)
            two = empire.snapshot(right, NOW + 1, session)
        one_points = {(round(fare["lat"], 5), round(fare["lng"], 5)) for fare in one["fares"]}
        for fare in two["fares"]:
            self.assertNotIn((round(fare["lat"], 5), round(fare["lng"], 5)), one_points)

    def test_tick_does_not_write_devices(self) -> None:
        with self.Session() as session:
            before = (
                session.scalar(select(func.count()).select_from(Device)),
                session.scalar(select(func.count()).select_from(LocationPing)),
                session.scalar(select(func.count()).select_from(DeviceLink)),
            )
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 20, session)
            after = (
                session.scalar(select(func.count()).select_from(Device)),
                session.scalar(select(func.count()).select_from(LocationPing)),
                session.scalar(select(func.count()).select_from(DeviceLink)),
            )
        self.assertEqual(before, after)

    def test_creating_ten_cars_does_not_insert_a_device(self) -> None:
        with self.Session() as session:
            before = session.scalar(select(func.count()).select_from(Device))
            empire.ensure_game("ada", NOW, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            after = session.scalar(select(func.count()).select_from(Device))
            columns = {col["name"] for col in inspect(session.get_bind()).get_columns("empire_cars")}
        signs = sorted((car.sign for car in cars), key=lambda sign: int(sign[1:]))
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        self.assertEqual(before, 0)
        self.assertEqual(after, 0)
        self.assertNotIn("device_id", columns)

    def test_ten_car_clock_uses_real_seconds(self) -> None:
        route = [[55.0, -1.5], [56.0, -1.5]]
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for car in cars:
                car.status = "to_passenger"
                car.route_json = "[[55.0, -1.5], [56.0, -1.5]]"
                car.duration_s = 100
                car.leg_started = NOW
                car.hold_until = NOW + 10000
            session.commit()
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
        expected = empire.point_at(route, 0.01)
        faster = empire.point_at(route, 0.02)
        for index in range(1, 11):
            car = self._car(body, f"T{index}")
            self.assertAlmostEqual(car["lat"], expected[0], places=4)
            self.assertAlmostEqual(car["lng"], expected[1], places=4)
            self.assertNotAlmostEqual(car["lat"], faster[0], places=4)
        self.assertFalse(empire.clock_running())

    def test_old_trail_points_are_dropped(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            car = session.scalars(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T1")).one()
            car.trail_json = f"[[-1.5, 55.03, {NOW - 601}]]"
            car.next_check = NOW
            session.commit()
            empire.tick_all(NOW, session)
            body = empire.snapshot("ada", NOW, session)
        trail = self._car(body, "T1")["trail"]
        self.assertTrue(trail)
        for point in trail:
            self.assertEqual(len(point), 3)
            self.assertGreaterEqual(point[2], NOW - 600)
            self.assertNotEqual(point[2], NOW - 601)

    def test_ten_car_pings_older_than_ten_minutes_leave_the_trail(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for index, car in enumerate(sorted(cars, key=lambda item: int(item.sign[1:])), start=1):
                kept_lng = round(-1.4 - index * 0.01, 6)
                kept_lat = round(55.1 + index * 0.01, 6)
                car.trail_json = (
                    f"[[{round(-1.5 - index * 0.01, 6)}, {round(55.0 + index * 0.01, 6)}, {NOW - 601}], "
                    f"[{kept_lng}, {kept_lat}, {NOW - 600}]]"
                )
                car.next_check = NOW
            session.commit()
            empire.tick_all(NOW, session)
            body = empire.snapshot("ada", NOW, session)
        self.assertEqual(len(body["cars"]), 10)
        for index in range(1, 11):
            trail = self._car(body, f"T{index}")["trail"]
            stamps = []
            for point in trail:
                self.assertEqual(len(point), 3)
                lng, lat, stamp = point
                self.assertLess(lng, 0)
                self.assertGreater(lat, 50)
                self.assertIs(type(stamp), int)
                self.assertGreaterEqual(stamp, NOW - 600)
                self.assertNotEqual(stamp, NOW - 601)
                stamps.append(stamp)
            self.assertIn(NOW - 600, stamps)
            self.assertIn(NOW, stamps)
            kept = next(point for point in trail if point[2] == NOW - 600)
            self.assertAlmostEqual(kept[0], -1.4 - index * 0.01, places=5)
            self.assertAlmostEqual(kept[1], 55.1 + index * 0.01, places=5)

    def test_ten_car_trail_point_is_longitude_then_latitude(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for index, car in enumerate(sorted(cars, key=lambda item: int(item.sign[1:])), start=1):
                car.status = "to_passenger"
                car.lat = 55.0 + index * 0.01
                car.lng = -1.5 - index * 0.01
                car.route_json = "[]"
                car.duration_s = 10000
                car.leg_started = NOW
                car.hold_until = NOW + 10000
                car.next_check = NOW
                car.trail_json = "[]"
            session.commit()
            empire.tick_all(NOW, session)
            body = empire.snapshot("ada", NOW, session)
        for index in range(1, 11):
            car = self._car(body, f"T{index}")
            self.assertEqual(len(car["trail"]), 1)
            lng, lat, stamp = car["trail"][0]
            self.assertAlmostEqual(lng, -1.5 - index * 0.01, places=5)
            self.assertAlmostEqual(lat, 55.0 + index * 0.01, places=5)
            self.assertAlmostEqual(lng, car["lng"], places=5)
            self.assertAlmostEqual(lat, car["lat"], places=5)
            self.assertNotAlmostEqual(lng, lat, places=3)
            self.assertEqual(stamp, NOW)

    def test_health_version_is_0_1_27(self) -> None:
        from app.config import VERSION
        from app.routers.health import health

        self.assertEqual(VERSION, "0.1.27")
        self.assertEqual(health()["version"], "0.1.27")

    def test_username_shifts_the_pickup(self) -> None:
        self.test_two_feeds_do_not_share_fare_coordinates()

    def test_username_shifts_the_ten_car_bearing(self) -> None:
        left = right = None
        drawn_left = drawn_right = None
        for index in range(80):
            one_name = f"bear{index}a"
            other_name = f"bear{index}b"
            one_draw = empire.draw_journey(one_name, NOW + 64, "person-0")
            other_draw = empire.draw_journey(other_name, NOW + 64, "person-0")
            if one_draw[1] == other_draw[1]:
                continue
            if empire.night_ends(one_name)[0] == empire.night_ends(other_name)[0]:
                continue
            left, right = one_name, other_name
            drawn_left, drawn_right = one_draw, other_draw
            break
        self.assertIsNotNone(left)
        self.assertIsNotNone(drawn_left)
        self.assertGreaterEqual(empire.trip_bearing(left), 160)
        self.assertLessEqual(empire.trip_bearing(left), 300)
        self.assertGreaterEqual(empire.trip_bearing(right), 160)
        self.assertLessEqual(empire.trip_bearing(right), 300)
        with self.Session() as session:
            empire.ensure_game(left, NOW, session)
            empire.ensure_game(right, NOW, session)
            empire.tick_all(NOW + 64, session)
            one = empire.snapshot(left, NOW + 64, session)
            other = empire.snapshot(right, NOW + 64, session)
            left_cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == left)).all())
            right_cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == right)).all())
        self.assertEqual(len(left_cars), 10)
        self.assertEqual(len(right_cars), 10)
        self.assertEqual(len(one["fares"]), 1)
        self.assertEqual(len(other["fares"]), 1)
        self.assertEqual(one["fare_in"], 0)
        self.assertEqual(other["fare_in"], 0)
        self.assertEqual((one["fares"][0]["lat"], one["fares"][0]["lng"]), drawn_left[1])
        self.assertEqual((other["fares"][0]["lat"], other["fares"][0]["lng"]), drawn_right[1])
        self.assertNotEqual(drawn_left[1], drawn_right[1])
        claimed = {one["fares"][0]["sign"], other["fares"][0]["sign"]}
        self.assertEqual(claimed, {"T1"})
        for cars, pin in ((left_cars, drawn_left[1]), (right_cars, drawn_right[1])):
            collectors = [car for car in cars if car.status == "to_passenger"]
            self.assertEqual([car.sign for car in collectors], ["T1"])
            self.assertAlmostEqual(collectors[0].fare_lat, pin[0], places=4)
            self.assertAlmostEqual(collectors[0].fare_lng, pin[1], places=4)
            for car in cars:
                if car.sign == "T1":
                    continue
                self.assertEqual(car.fare_name, "")
                self.assertEqual((car.fare_lat, car.fare_lng), (0.0, 0.0))
                self.assertNotEqual(car.status, "with_passenger")

    def test_names_cycle(self) -> None:
        def quick(start, end):
            return {"points": [[start[0], start[1]], [end[0], end[1]]], "duration_s": 1}

        empire.set_route_fn(quick)
        seen = []
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all():
                if car.sign != "T1":
                    car.hold_until = NOW + 100000
            session.commit()
            for step in range(0, 60):
                empire.tick_all(NOW + step, session)
                body = empire.snapshot("ada", NOW + step, session)
                fares = [fare for fare in body["fares"] if fare["sign"] == "T1"]
                if fares and (not seen or seen[-1] != fares[0]["name"]):
                    seen.append(fares[0]["name"])
        self.assertTrue(seen)
        self.assertEqual(seen[0], "Mina")
        self.assertTrue(all(name in ("Mina", "Owen", "Priya", "Jack", "Helen", "Tom") for name in seen))
        game = empire.EmpireGame(username="ada", created_at="0", name_index=0, cash_pence=0)
        self.assertEqual(
            [empire._next_name(game) for _ in range(6)],
            ["Mina", "Owen", "Priya", "Jack", "Helen", "Tom"],
        )

    def test_ten_car_names_cycle_mina_through_tom(self) -> None:
        cycle = ("Mina", "Owen", "Priya", "Jack", "Helen", "Tom")
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            empire.tick_all(DAY + 64, session)
            body = empire.snapshot("ada", DAY + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        names = [car["passenger"] for car in body["cars"] if car["passenger"]]
        self.assertGreaterEqual(len(names), 1)
        self.assertLess(len(names), 10)
        self.assertEqual(names[0], "Mina")
        self.assertTrue(all(name in cycle for name in names))
        self.assertEqual(self._car(body, "T1")["passenger"], "Mina")

    def test_collected_ten_car_fare_leaves_the_feed(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            empire.tick_all(DAY + 64, session)
            waiting = empire.snapshot("ada", DAY + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            collecting = [car.sign for car in cars if car.status == "to_passenger" and car.fare_name]
            for car in cars:
                if car.sign not in collecting:
                    continue
                car.duration_s = 10
                car.leg_started = DAY
                car.route_json = "[[55.0367, -1.5075], [55.04, -1.51]]"
            session.commit()
            empire.tick_all(DAY + 10, session)
            arrived = empire.snapshot("ada", DAY + 10, session)
        self.assertGreaterEqual(len(waiting["fares"]), 1)
        self.assertEqual({fare["sign"] for fare in waiting["fares"]}, set(collecting))
        self.assertLess(len(collecting), 10)
        self.assertEqual(arrived["fares"], [])
        for sign in collecting:
            self.assertEqual(self._car(arrived, sign)["status"], "with_passenger")
        for car in arrived["cars"]:
            if car["sign"] not in collecting:
                self.assertNotEqual(car["status"], "with_passenger")

    def test_ten_car_fare_is_present_only_while_going_to_the_passenger(self) -> None:
        statuses = ("at_depot", "to_passenger", "with_passenger", "returning")
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for car in cars:
                car.status = statuses[(int(car.sign[1:]) - 1) % 4]
                car.fare_name = "Mina"
                car.passenger = "Mina"
                car.fare_lat = 55.04
                car.fare_lng = -1.51
            session.commit()
            body = empire.snapshot("ada", NOW, session)
        self.assertEqual(len(body["cars"]), 10)
        fare_signs = {fare["sign"] for fare in body["fares"]}
        expected = set()
        for car in body["cars"]:
            if car["status"] == "to_passenger":
                expected.add(car["sign"])
                self.assertIn(car["sign"], fare_signs)
            else:
                self.assertNotIn(car["sign"], fare_signs)
        self.assertEqual(fare_signs, expected)
        self.assertEqual(fare_signs, {"T2", "T6", "T10"})
        for fare in body["fares"]:
            self.assertEqual(fare["name"], "Mina")

    def test_depot_car_still_has_a_position(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 10, session)
            body = empire.snapshot("ada", NOW + 10, session)
        car = self._car(body, "T3")
        self.assertEqual(car["status"], "at_depot")
        self.assertAlmostEqual(car["lat"], 55.0367, places=4)
        self.assertAlmostEqual(car["lng"], -1.5075, places=4)
        self.assertEqual(len(car["trail"]), 1)

    def test_one_car_fault_does_not_stop_other_accounts(self) -> None:
        empire.set_fault(lambda car: car.username == "ada" and car.sign == "T1")
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.ensure_game("bee", NOW, session)
            with self.assertLogs("hackathon-api", level="ERROR"):
                empire.tick_all(NOW + 1, session)
            ada = empire.snapshot("ada", NOW + 1, session)
            bee = empire.snapshot("bee", NOW + 1, session)
        self.assertEqual(self._car(ada, "T1")["status"], "at_depot")
        self.assertEqual(self._car(bee, "T1")["status"], "to_passenger")

    def test_one_ten_car_fault_does_not_stop_the_other_nine(self) -> None:
        empire.set_fault(lambda car: car.sign == "T4")
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            with self.assertLogs("hackathon-api", level="ERROR"):
                empire.tick_all(DAY + 64, session)
            body = empire.snapshot("ada", DAY + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(self._car(body, "T4")["status"], "at_depot")
        self.assertEqual(self._car(body, "T1")["status"], "to_passenger")
        self.assertNotIn("T11", [car["sign"] for car in body["cars"]])

    def test_first_departures_are_staggered_by_about_7_seconds(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            empire.tick_all(DAY + 1, session)
            early = empire.snapshot("ada", DAY + 1, session)
            empire.tick_all(DAY + 8, session)
            middle = empire.snapshot("ada", DAY + 8, session)
            empire.tick_all(DAY + 15, session)
            late = empire.snapshot("ada", DAY + 15, session)
        self.assertEqual(self._car(early, "T1")["status"], "to_passenger")
        self.assertEqual(self._car(early, "T2")["status"], "at_depot")
        self.assertEqual(self._car(early, "T2")["job"], "waiting")
        self.assertEqual(self._car(early, "T3")["status"], "at_depot")
        self.assertEqual(self._car(middle, "T3")["status"], "at_depot")
        self.assertEqual(self._car(middle, "T3")["job"], "waiting")
        if self._car(middle, "T2")["status"] == "at_depot":
            self.assertEqual(self._car(middle, "T2")["job"], "on break")
        if self._car(late, "T3")["status"] == "at_depot":
            self.assertEqual(self._car(late, "T3")["job"], "on break")

    def test_bearings_stay_inland(self) -> None:
        low = high = mid = None
        for i in range(5000):
            name = f"n{i}"
            raw = empire.raw_bearing(name)
            if low is None and raw < 160:
                low = name
            elif high is None and raw > 300:
                high = name
            elif mid is None and 160 <= raw <= 300:
                mid = name
            if low and high and mid:
                break
        self.assertEqual(empire.trip_bearing(low), 160)
        self.assertNotEqual(empire.raw_bearing(low), 160)
        self.assertEqual(empire.trip_bearing(high), 300)
        self.assertNotEqual(empire.raw_bearing(high), 300)
        self.assertEqual(empire.trip_bearing(mid), empire.raw_bearing(mid))
        self.assertGreaterEqual(empire.trip_bearing(mid), 160)
        self.assertLessEqual(empire.trip_bearing(mid), 300)

    def test_pickup_and_drop_stay_inside_the_circle(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            car = session.scalars(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T1")).one()
            pickup = (car.fare_lat, car.fare_lng)
            drop = (car.drop_lat, car.drop_lng)
        places = {(lat, lng) for _name, lat, lng in empire.PLACES}
        ring_pickup, ring_drop = _ring("ada")
        self.assertIn(pickup, places)
        self.assertIn(drop, places)
        self.assertNotEqual(pickup, ring_pickup)
        self.assertNotEqual(drop, ring_drop)
        self.assertTrue(empire.inside_circle(pickup))
        self.assertTrue(empire.inside_circle(drop))
        self.assertGreater(empire.haversine_km(pickup, empire.BERWICK), 40)
        self.assertGreater(empire.haversine_km(drop, empire.BERWICK), 40)

    def test_ten_car_trips_stay_inside_the_circle(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            empire.tick_all(DAY + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
        self.assertEqual(len(cars), 10)
        claimed = [car for car in cars if car.passenger]
        self.assertGreaterEqual(len(claimed), 1)
        self.assertLess(len(claimed), 10)
        for car in claimed:
            pickup = (car.fare_lat, car.fare_lng)
            drop = (car.drop_lat, car.drop_lng)
            self.assertTrue(empire.inside_circle(pickup), car.sign)
            self.assertTrue(empire.inside_circle(drop), car.sign)
            self.assertNotEqual(pickup, empire.BERWICK)
            self.assertNotEqual(drop, empire.BERWICK)
            self.assertGreater(empire.haversine_km(pickup, empire.BERWICK), 40)
            self.assertGreater(empire.haversine_km(drop, empire.BERWICK), 40)

    def test_ten_car_ends_are_places_not_the_depot_ring(self) -> None:
        _kind, expected_pickup, expected_drop = empire.draw_journey("ada", NOW + 64, "person-0")
        ring_pickup, ring_drop = _ring("ada")
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            people = list(session.scalars(select(empire.EmpirePerson).where(empire.EmpirePerson.username == "ada")).all())
        self.assertEqual(len(cars), 10)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0].sign, "T1")
        self.assertNotEqual(expected_pickup, ring_pickup)
        self.assertNotEqual(expected_drop, ring_drop)
        collectors = [car for car in cars if car.status == "to_passenger"]
        self.assertEqual([car.sign for car in collectors], ["T1"])
        pickup = (collectors[0].fare_lat, collectors[0].fare_lng)
        drop = (collectors[0].drop_lat, collectors[0].drop_lng)
        self.assertEqual((pickup, drop), (expected_pickup, expected_drop))
        self.assertEqual(pickup, (people[0].lat, people[0].lng))
        self.assertTrue(empire.inside_circle(pickup))
        self.assertTrue(empire.inside_circle(drop))
        for car in cars:
            if car.sign == "T1":
                continue
            self.assertEqual(car.passenger, "")
            self.assertEqual(car.fare_name, "")
            self.assertEqual((car.fare_lat, car.fare_lng), (0.0, 0.0), car.sign)
            if car.status == "to_better_spot":
                route = empire._loads(car.route_json, [])
                end = (route[-1][0], route[-1][1])
                self.assertNotEqual(end, pickup, car.sign)
                self.assertEqual(end, empire.strategic_spot("ada", car.sign, NOW + 64), car.sign)

    def test_import_does_not_start_the_clock_or_open_the_live_database(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        live = ROOT / "hackathon.db"
        before = live.stat().st_mtime if live.exists() else None
        self.assertFalse(empire.clock_running())
        with TestClient(app) as client:
            self.assertTrue(empire.clock_running())
            self.assertEqual(client.get("/health").json()["version"], "0.1.27")
        self.assertFalse(empire.clock_running())
        after = live.stat().st_mtime if live.exists() else None
        self.assertEqual(before, after)
        self.assertNotEqual(Path(TEMP_DB).resolve(), live.resolve())

    def test_game_survives_a_new_session(self) -> None:
        path = Path(tempfile.mkstemp(prefix="empire-keep-", suffix=".db")[1])
        engine = create_engine("sqlite:///" + str(path).replace("\\", "/"))
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as first:
            empire.ensure_game("ada", NOW, first)
        with Session() as second:
            signs = sorted(
                (car.sign for car in second.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all()),
                key=lambda sign: int(sign[1:]),
            )
        self.assertEqual(signs, [f"T{index}" for index in range(1, 11)])
        self.assertNotEqual(path.resolve(), (ROOT / "hackathon.db").resolve())
        engine.dispose()

    def test_get_ensures_a_game_for_an_old_account(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app
        from app.models import User
        from app.schemas import UserCreate

        User.create(UserCreate(username="oldaccount", password="password1"))
        with SessionLocal() as session:
            self.assertIsNone(session.get(EmpireGame, "oldaccount"))
        token = User.get("oldaccount").issue_token().access_token
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/empire").status_code, 401)
            body = client.get("/v1/empire", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(body["username"], "oldaccount")
        self.assertEqual([car["sign"] for car in body["cars"]], [f"T{index}" for index in range(1, 11)])

    def test_feed_colours(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW, session)
        colours = {car["sign"]: car["colour"] for car in body["cars"]}
        self.assertEqual(
            colours,
            {
                "T1": "#f5b942",
                "T2": "#38bdf8",
                "T3": "#4ade80",
                "T4": "#c084fc",
                "T5": "#fb7185",
                "T6": "#f97316",
                "T7": "#a3e635",
                "T8": "#22d3ee",
                "T9": "#e879f9",
                "T10": "#facc15",
            },
        )

    def test_position_is_the_fraction_of_the_route(self) -> None:
        route = [[55.0, -1.5], [55.2, -1.5], [55.4, -1.5]]
        self.assertEqual(empire.point_at(route, 0), (55.0, -1.5))
        self.assertEqual(empire.point_at(route, 1), (55.4, -1.5))
        mid = empire.point_at(route, 0.5)
        self.assertAlmostEqual(mid[0], 55.2, places=4)
        self.assertAlmostEqual(mid[1], -1.5, places=4)
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            car = session.scalars(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T1")).one()
            car.status = "to_passenger"
            car.route_json = "[[55.0, -1.5], [55.2, -1.5], [55.4, -1.5]]"
            car.duration_s = 100
            car.leg_started = NOW
            car.hold_until = NOW + 1000
            session.commit()
            empire.tick_all(NOW + 50, session)
            body = empire.snapshot("ada", NOW + 50, session)
        moved = self._car(body, "T1")
        self.assertAlmostEqual(moved["lat"], 55.2, places=3)
        self.assertAlmostEqual(moved["lng"], -1.5, places=3)

    def test_many_ticks_leave_one_game_row(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            for step in range(40):
                empire.tick_all(NOW + step, session)
            self.assertEqual(empire.game_count("ada", session), 1)

    def test_a_quiet_second_does_not_append_a_trail_point(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 10, session)
            empire.tick_all(NOW + 11, session)
            body = empire.snapshot("ada", NOW + 11, session)
        self.assertEqual(len(self._car(body, "T1")["trail"]), 1)
        self.assertEqual(self._car(body, "T1")["trail"][0][2], NOW + 10)

    def test_ten_car_check_in_appends_one_trail_point(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 10, session)
            checked = empire.snapshot("ada", NOW + 10, session)
            empire.tick_all(NOW + 11, session)
            quiet = empire.snapshot("ada", NOW + 11, session)
        self.assertEqual(len(checked["cars"]), 10)
        for index in range(1, 11):
            sign = f"T{index}"
            first = self._car(checked, sign)["trail"]
            second = self._car(quiet, sign)["trail"]
            self.assertEqual(len(first), 1, sign)
            self.assertEqual(second, first, sign)
            lng, lat, stamp = first[0]
            self.assertEqual(len(first[0]), 3)
            self.assertLess(lng, 0)
            self.assertGreater(lat, 50)
            self.assertEqual(stamp, NOW + 10)
            self.assertNotEqual(stamp, NOW + 11)

    def test_three_cars_can_hold_three_statuses(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            cars = {car.sign: car for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all()}
            cars["T1"].status = "to_passenger"
            cars["T1"].fare_name = "Mina"
            cars["T1"].passenger = "Mina"
            cars["T2"].status = "with_passenger"
            cars["T2"].passenger = "Owen"
            cars["T3"].status = "returning"
            session.commit()
            body = empire.snapshot("ada", NOW, session)
        statuses = {car["sign"]: car["status"] for car in body["cars"]}
        self.assertEqual(statuses["T1"], "to_passenger")
        self.assertEqual(statuses["T2"], "with_passenger")
        self.assertEqual(statuses["T3"], "returning")
        for sign, status in statuses.items():
            if sign not in {"T1", "T2", "T3"}:
                self.assertEqual(status, "at_depot", sign)

    def test_feed_includes_the_money_lines(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW, session)
        for key in ("fare_in", "driver_share", "fuel", "standing"):
            self.assertIs(type(body[key]), int)
        self.assertEqual(body["fare_in"], 0)
        self.assertEqual(body["driver_share"], 0)
        self.assertEqual(body["fuel"], 0)
        self.assertEqual(body["standing"], 1500 * 10)

    def test_driver_share_is_half_the_fare(self) -> None:
        self.assertEqual(empire.driver_share_pence(1040), 520)
        lines = empire.money_lines(10, fare_in=1040)
        self.assertEqual(lines["fare_in"], 1040)
        self.assertEqual(lines["driver_share"], 520)
        self.assertIs(type(lines["driver_share"]), int)
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW, session)
        self.assertEqual(body["driver_share"], body["fare_in"] // 2)

    def test_fuel_is_twenty_pence_a_mile(self) -> None:
        self.assertEqual(empire.FUEL_PENCE_PER_MILE, 20)
        self.assertEqual(empire.fuel_pence(7), 140)
        three = empire.destination(empire.DEPOT, 80, 3.0)
        one = empire.destination(empire.DEPOT, 80, 1.0)
        with self.Session() as session:
            game = empire.ensure_game("milefuel", NOW, session)
            empire.ensure_game("mileother", NOW, session)
            car = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "milefuel", EmpireCar.sign == "T1")
            ).one()
            car.status = "with_passenger"
            car.passenger = "Mina"
            car.route_json = json.dumps([[empire.DEPOT[0], empire.DEPOT[1]], [three[0], three[1]]])
            car.duration_s = 360
            car.leg_started = NOW
            empire.advance_car(car, game, NOW + 361)
            session.commit()
            done = empire.snapshot("milefuel", NOW + 361, session)
            self.assertEqual(done["fuel"], empire.fuel_pence(3))
            second = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "milefuel", EmpireCar.sign == "T2")
            ).one()
            second.status = "to_better_spot"
            second.route_json = json.dumps([[empire.DEPOT[0], empire.DEPOT[1]], [one[0], one[1]]])
            second.duration_s = 120
            second.leg_started = NOW
            session.commit()
            half = empire.snapshot("milefuel", NOW + 60, session)
            rival = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("milefuel", "north"),
                    EmpireCar.sign == "N1",
                )
            ).one()
            rival.status = "with_passenger"
            rival.firm = "north"
            rival.passenger = "Priya"
            rival.route_json = json.dumps([[empire.DEPOT[0], empire.DEPOT[1]], [three[0], three[1]]])
            rival.duration_s = 360
            rival.leg_started = NOW
            empire.advance_car(rival, game, NOW + 361)
            session.commit()
            self.assertEqual(empire.buy_car("milefuel", 800000, NOW, session), "T11")
            bought = empire.snapshot("milefuel", NOW + 60, session)
            other = empire.snapshot("mileother", NOW + 60, session)
        self.assertEqual(empire.FUEL_PENCE_PER_MILE, 20)
        self.assertEqual(half["fuel"], empire.fuel_pence(3.5))
        self.assertEqual(bought["fuel"], half["fuel"])
        self.assertNotEqual(bought["fuel"], 800000)
        self.assertEqual(bought["new_car"], 800000)
        self.assertEqual(len(bought["cars"]), 11)
        self.assertEqual(other["fuel"], 0)
        self.assertEqual(other["cash_pence"], 0)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())
        lines = empire.money_lines(10, miles=7)
        self.assertEqual(lines["fuel"], 140)
        self.assertIs(type(lines["fuel"]), int)

    def test_standing_is_1500_pence_a_car(self) -> None:
        self.assertEqual(empire.STANDING_PENCE, 1500)
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW, session)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(body["standing"], 1500 * len(body["cars"]))
        self.assertIs(type(body["standing"]), int)

    def test_standing_does_not_rise_when_the_ten_drive(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            before = empire.snapshot("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            miles = sum(empire.driven_miles(car, NOW + 64) for car in cars)
            body = empire.snapshot("ada", NOW + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(before["fuel"], 0)
        self.assertEqual(before["standing"], 1500 * 10)
        self.assertGreater(miles, 0)
        self.assertEqual(body["fuel"], empire.fuel_pence(miles))
        self.assertGreater(body["fuel"], 0)
        self.assertEqual(body["standing"], 1500 * 10)
        self.assertNotEqual(body["standing"], before["standing"] + body["fuel"])

    def test_ten_cars_standing_sum_to_minus_15000(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW, session)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(body["fare_in"], 0)
        self.assertEqual(body["fuel"], 0)
        day = body["fare_in"] - body["driver_share"] - body["fuel"] - body["standing"]
        self.assertEqual(day, -15000)

        def integers(value):
            if type(value) is int:
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from integers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from integers(item)

        self.assertNotIn(2860, list(integers(body)))

    def test_new_car_costs_800000_before_it_leaves(self) -> None:
        self.assertEqual(empire.NEW_CAR_PENCE, 800000)
        self.assertFalse(empire.new_car_may_leave(0))
        self.assertFalse(empire.new_car_may_leave(799999))
        self.assertTrue(empire.new_car_may_leave(800000))
        with self.Session() as session:
            before = session.scalar(select(func.count()).select_from(Device))
            empire.ensure_game("ada", NOW, session)
            session.add(
                EmpireCar(
                    username="ada",
                    sign="T11",
                    colour="#ffffff",
                    status="at_depot",
                    passenger="",
                    lat=55.0367,
                    lng=-1.5075,
                    route_json="[]",
                    duration_s=0,
                    leg_started=NOW,
                    osrm=False,
                    trail_json="[]",
                    fare_name="",
                    hold_until=NOW,
                    next_check=NOW + empire.CHECK_S,
                    depart_offset=-1,
                )
            )
            session.commit()
            extra = session.scalars(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T11")).one()
            self.assertFalse(empire.pay_new_car(extra, 799999))
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
            self.assertEqual(body["new_car"], 800000)
            self.assertIs(type(body["new_car"]), int)
            self.assertEqual(self._car(body, "T11")["status"], "at_depot")
            self.assertEqual(self._car(body, "T1")["status"], "to_passenger")
            after = session.scalar(select(func.count()).select_from(Device))
            self.assertEqual(before, after)
            extra = session.scalars(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T11")).one()
            self.assertTrue(empire.pay_new_car(extra, 800000))
            session.commit()
            empire.tick_all(NOW + 2, session)
            left = empire.snapshot("ada", NOW + 2, session)
        self.assertNotEqual(self._car(left, "T11")["status"], "to_passenger")
        self.assertEqual(self._car(left, "T11")["passenger"], "")
        self.assertEqual(self._car(left, "T1")["status"], "to_passenger")

    def test_new_car_800000_is_not_added_to_fare_in(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            session.add(
                EmpireCar(
                    username="ada",
                    sign="T11",
                    colour="#ffffff",
                    status="at_depot",
                    passenger="",
                    lat=55.0367,
                    lng=-1.5075,
                    route_json="[]",
                    duration_s=0,
                    leg_started=NOW,
                    osrm=False,
                    trail_json="[]",
                    fare_name="",
                    hold_until=NOW,
                    next_check=NOW + empire.CHECK_S,
                    depart_offset=-1,
                )
            )
            session.commit()
            extra = session.scalars(select(EmpireCar).where(EmpireCar.username == "ada", EmpireCar.sign == "T11")).one()
            self.assertTrue(empire.pay_new_car(extra, 800000))
            session.commit()
            empire.tick_all(NOW + 2, session)
            body = empire.snapshot("ada", NOW + 2, session)
        self.assertEqual(body["new_car"], 800000)
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertNotEqual(body["fare_in"], body["new_car"])

    def test_ten_car_feed_hides_the_company_totals(self) -> None:
        banned = {2860, 1130, 1240}

        def integers(value):
            if type(value) is int:
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from integers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from integers(item)

        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
        self.assertEqual(len(body["cars"]), 10)
        self.assertTrue(banned.isdisjoint(set(integers(body))))

    def test_driven_ten_car_feed_omits_minus_1120(self) -> None:
        def integers(value):
            if type(value) is int:
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from integers(item)
            elif isinstance(value, list):
                for item in value:
                    yield from integers(item)

        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        self.assertGreater(body["fuel"], 0)
        self.assertNotIn(-1120, list(integers(body)))
        self.assertNotIn("-1120", str(body))

    def test_feed_names_the_clock_and_the_trail(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW + 3, session)
        self.assertEqual(
            set(body),
            {
                "ok",
                "username",
                "server_now",
                "check_s",
                "trail_s",
                "osrm_unavailable",
                "cars",
                "fares",
                "people",
                "fare_in",
                "driver_share",
                "fuel",
                "standing",
                "new_car",
                "cash_pence",
                "firms",
                "loiter",
            },
        )
        self.assertIsNone(body["loiter"])
        self.assertEqual(body["cash_pence"], 0)
        self.assertIs(type(body["cash_pence"]), int)
        self.assertEqual(body["fare_in"], 0)
        self.assertTrue(body["ok"])
        self.assertEqual(body["check_s"], 10)
        self.assertEqual(body["trail_s"], 600)
        self.assertEqual(body["server_now"], NOW + 3)
        self.assertEqual(body["username"], "ada")

    def test_osrm_flag_follows_the_current_leg(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            missed = empire.snapshot("ada", NOW + 1, session)
            self.assertTrue(missed["osrm_unavailable"])

            def answered(start, end):
                return {"points": [[start[0], start[1]], [end[0], end[1]]], "duration_s": 50}

            empire.set_route_fn(answered)
            empire.ensure_game("bee", NOW, session)
            empire.tick_all(NOW + 1, session)
            hit = empire.snapshot("bee", NOW + 1, session)
        self.assertFalse(hit["osrm_unavailable"])
        self.assertTrue(self._car(hit, "T1")["osrm"])

    def test_osrm_unavailable_is_true_when_any_of_the_ten_missed(self) -> None:
        def answered(start, end):
            return {"points": [[start[0], start[1]], [end[0], end[1]]], "duration_s": 80}

        empire.set_route_fn(answered)
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            empire.tick_all(DAY + 64, session)
            hit = empire.snapshot("ada", DAY + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            missed = next(car for car in cars if car.status != "at_depot")
            missed_sign = missed.sign
            missed.osrm = False
            session.commit()
            mixed = empire.snapshot("ada", DAY + 64, session)
        self.assertEqual(len(hit["cars"]), 10)
        self.assertFalse(hit["osrm_unavailable"])
        movers = [car for car in hit["cars"] if car["status"] != "at_depot"]
        self.assertTrue(any(car["status"] == "to_passenger" for car in movers))
        for car in movers:
            self.assertTrue(car["osrm"])
        self.assertTrue(mixed["osrm_unavailable"])
        self.assertFalse(self._car(mixed, missed_sign)["osrm"])

    def test_a_leaving_leg_starts_at_the_depot(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
        start = self._car(body, "T1")["route"][0]
        self.assertAlmostEqual(start[0], 55.0367, places=4)
        self.assertAlmostEqual(start[1], -1.5075, places=4)

    def test_leaving_ten_car_routes_start_at_the_depot(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", DAY, session)
            empire.tick_all(DAY + 64, session)
            body = empire.snapshot("ada", DAY + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        movers = [car for car in body["cars"] if car["status"] != "at_depot"]
        self.assertTrue(any(car["status"] == "to_passenger" for car in movers))
        for car in movers:
            start = car["route"][0]
            self.assertAlmostEqual(start[0], empire.DEPOT[0], places=4)
            self.assertAlmostEqual(start[1], empire.DEPOT[1], places=4)

    def test_status_is_one_of_four_words(self) -> None:
        allowed = {"at_depot", "to_passenger", "with_passenger", "returning"}
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
        seen = {car["status"] for car in body["cars"]}
        self.assertTrue(seen <= allowed)
        self.assertIn("to_passenger", seen)
        self.assertIn("at_depot", seen)

    def test_each_of_the_ten_uses_one_of_four_statuses(self) -> None:
        allowed = ("at_depot", "to_passenger", "with_passenger", "returning")
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            driving = empire.snapshot("ada", NOW + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for car in cars:
                car.status = allowed[(int(car.sign[1:]) - 1) % 4]
            session.commit()
            mixed = empire.snapshot("ada", NOW, session)
        self.assertEqual(len(driving["cars"]), 10)
        driving_status = {car["status"] for car in driving["cars"]}
        self.assertTrue(driving_status <= set(empire.STATUSES))
        self.assertIn("to_passenger", driving_status)
        self.assertIn("at_depot", driving_status)
        self.assertEqual({car["status"] for car in mixed["cars"]}, set(allowed))
        for car in mixed["cars"]:
            self.assertIn(car["status"], allowed)

    def test_place_table_names_real_positions_inside_the_circle(self) -> None:
        expected = (
            "Shiremoor",
            "Backworth",
            "Earsdon",
            "West Monkseaton",
            "Forest Hall",
            "Killingworth",
            "Benton",
            "Cobalt",
            "Silverlink",
            "Royal Quays",
            "Regent Centre",
            "Four Lane Ends",
            "Whitley Bay",
            "Spanish City",
            "Cullercoats",
            "Tynemouth",
            "North Shields",
            "Grey's Monument",
            "Central Station",
            "Haymarket",
            "the Quayside",
            "Osborne Road",
            "North Tyneside General at Rake Lane",
            "the RVI",
            "the Freeman",
            "Newcastle Airport",
        )
        calls = []

        def trap(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("network")

        original = urllib.request.urlopen
        urllib.request.urlopen = trap
        try:
            places = tuple(empire.PLACES)
        finally:
            urllib.request.urlopen = original
        self.assertEqual(calls, [])
        self.assertEqual(tuple(name for name, _lat, _lng in places), expected)
        self.assertEqual(len(places), len(set(expected)))
        points = {(lat, lng) for _name, lat, lng in places}
        self.assertEqual(len(points), len(places))
        self.assertNotIn(empire.BERWICK, points)
        for name, lat, lng in places:
            point = (lat, lng)
            self.assertTrue(empire.inside_circle(point), name)
            self.assertGreater(empire.haversine_km(point, empire.BERWICK), 40, name)
            self.assertNotIn("Berwick", name)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_morning_journey_runs_from_homes_toward_town(self) -> None:
        homes = {empire.place_point(name) for name in empire.HOMES}
        drops = {empire.place_point(name) for name in empire.MORNING_DROPS}
        name = None
        for index in range(80):
            candidate = f"morn{index}"
            if empire.morning_ends(candidate) != empire.journey_ends(candidate):
                name = candidate
                break
        self.assertIsNotNone(name)
        winter = datetime(2023, 11, 15, 8, 0, tzinfo=timezone.utc).timestamp()
        summer = datetime(2024, 7, 15, 7, 0, tzinfo=timezone.utc).timestamp()
        opening = datetime(2023, 11, 15, 7, 0, tzinfo=timezone.utc).timestamp()
        late = datetime(2023, 11, 15, 9, 29, tzinfo=timezone.utc).timestamp()
        before = datetime(2023, 11, 15, 6, 59, tzinfo=timezone.utc).timestamp()
        edge = datetime(2023, 11, 15, 9, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(empire.london_clock(winter), (8, 0))
        self.assertEqual(empire.london_clock(summer), (8, 0))
        self.assertTrue(empire.is_morning(winter))
        self.assertTrue(empire.is_morning(summer))
        self.assertTrue(empire.is_morning(opening))
        self.assertTrue(empire.is_morning(late))
        self.assertFalse(empire.is_morning(before))
        self.assertFalse(empire.is_morning(edge))
        self.assertEqual(empire.london_clock(datetime(2024, 3, 31, 0, 30, tzinfo=timezone.utc).timestamp()), (0, 30))
        self.assertEqual(empire.london_clock(datetime(2024, 3, 31, 1, 0, tzinfo=timezone.utc).timestamp()), (2, 0))
        self.assertEqual(empire.london_clock(datetime(2024, 10, 27, 0, 30, tzinfo=timezone.utc).timestamp()), (1, 30))
        self.assertEqual(empire.london_clock(datetime(2024, 10, 27, 1, 0, tzinfo=timezone.utc).timestamp()), (1, 0))

        def claim(account: str, stamp: float) -> tuple[str, tuple[float, float], tuple[float, float], int]:
            engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            with Session() as session:
                empire.ensure_game(account, stamp, session)
                empire.tick_all(stamp + 1, session)
                rows = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
                car = next(row for row in rows if row.sign == "T1")
                people = session.scalars(select(empire.EmpirePerson).where(empire.EmpirePerson.username == account)).all()
                self.assertEqual(len(rows), 10, account)
                return car.status, (car.fare_lat, car.fare_lng), (car.drop_lat, car.drop_lng), len(list(people))

        for stamp, account in (
            (winter, name),
            (summer, "summermorn"),
            (opening, "openmorn"),
            (late, "latemorn"),
        ):
            status, pickup, drop, people = claim(account, stamp)
            drawn = _draws_nearest_depot(account, stamp + 1, people)[0]
            self.assertEqual(status, "to_passenger", account)
            self.assertGreater(people, 10, account)
            self.assertEqual((pickup, drop), drawn[1:], account)
            self.assertIn(pickup, places := {(lat, lng) for _n, lat, lng in empire.PLACES}, account)
            self.assertIn(drop, places, account)
            self.assertTrue(empire.inside_circle(pickup), account)
            self.assertTrue(empire.inside_circle(drop), account)
        self.assertGreater(empire.spell_target("beforemorn", before), -1)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_day_journey_uses_the_coast_hospitals_and_town(self) -> None:
        places = {empire.place_point(name) for name in empire.DAY_PLACES}
        self.assertEqual(
            empire.DAY_PLACES,
            (
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
            ),
        )
        name = None
        for index in range(80):
            candidate = f"day{index}"
            if empire.day_ends(candidate) != empire.journey_ends(candidate):
                name = candidate
                break
        self.assertIsNotNone(name)
        noon = datetime(2023, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp()
        summer = datetime(2024, 7, 15, 12, 0, tzinfo=timezone.utc).timestamp()
        opening = datetime(2023, 11, 15, 9, 30, tzinfo=timezone.utc).timestamp()
        late = datetime(2023, 11, 15, 16, 29, tzinfo=timezone.utc).timestamp()
        summer_open = datetime(2024, 7, 15, 8, 30, tzinfo=timezone.utc).timestamp()
        before = datetime(2023, 11, 15, 9, 29, tzinfo=timezone.utc).timestamp()
        edge = datetime(2023, 11, 15, 16, 30, tzinfo=timezone.utc).timestamp()
        summer_edge = datetime(2024, 7, 15, 15, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(empire.london_clock(summer), (13, 0))
        self.assertEqual(empire.london_clock(summer_open), (9, 30))
        self.assertEqual(empire.london_clock(summer_edge), (16, 30))
        self.assertTrue(empire.is_day(noon))
        self.assertTrue(empire.is_day(summer))
        self.assertTrue(empire.is_day(opening))
        self.assertTrue(empire.is_day(late))
        self.assertTrue(empire.is_day(summer_open))
        self.assertFalse(empire.is_day(before))
        self.assertFalse(empire.is_day(edge))
        self.assertFalse(empire.is_day(summer_edge))
        self.assertTrue(empire.is_morning(before))

        def claim(account: str, stamp: float) -> tuple[str, tuple[float, float], tuple[float, float], int]:
            engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            with Session() as session:
                empire.ensure_game(account, stamp, session)
                empire.tick_all(stamp + 1, session)
                rows = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
                car = next(row for row in rows if row.sign == "T1")
                self.assertEqual(len(rows), 10, account)
                return car.status, (car.fare_lat, car.fare_lng), (car.drop_lat, car.drop_lng), len(rows)

        for stamp, account in (
            (noon, name),
            (summer, "daysummer"),
            (opening, "dayopen"),
            (late, "daylate"),
            (summer_open, "daybst"),
        ):
            status, pickup, drop, count = claim(account, stamp)
            drawn = empire.draw_journey(account, stamp + 1, "person-0")
            self.assertEqual(status, "to_passenger", account)
            self.assertEqual(count, 10, account)
            self.assertEqual((pickup, drop), drawn[1:], account)
            self.assertNotEqual(pickup, drop, account)
            self.assertTrue(empire.inside_circle(pickup), account)
            self.assertTrue(empire.inside_circle(drop), account)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_evening_journey_runs_from_town_back_to_the_homes(self) -> None:
        pickups = {empire.place_point(name) for name in empire.EVENING_PICKUPS}
        drops = {empire.place_point(name) for name in empire.EVENING_DROPS}
        self.assertTrue(set(empire.EVENING_PICKUPS).isdisjoint(empire.EVENING_DROPS))
        self.assertEqual(empire.EVENING_PICKUPS, ("Cobalt",) + empire.TOWN)
        self.assertEqual(empire.EVENING_DROPS, empire.HOMES + empire.COAST)
        name = None
        for index in range(80):
            candidate = f"eve{index}"
            if empire.evening_ends(candidate) != empire.journey_ends(candidate):
                name = candidate
                break
        self.assertIsNotNone(name)
        opening = datetime(2023, 11, 15, 16, 30, tzinfo=timezone.utc).timestamp()
        middle = datetime(2023, 11, 15, 18, 0, tzinfo=timezone.utc).timestamp()
        late = datetime(2023, 11, 15, 19, 29, tzinfo=timezone.utc).timestamp()
        summer = datetime(2024, 7, 15, 15, 30, tzinfo=timezone.utc).timestamp()
        summer_late = datetime(2024, 7, 15, 18, 29, tzinfo=timezone.utc).timestamp()
        before = datetime(2023, 11, 15, 16, 29, tzinfo=timezone.utc).timestamp()
        edge = datetime(2023, 11, 15, 19, 30, tzinfo=timezone.utc).timestamp()
        summer_edge = datetime(2024, 7, 15, 18, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(empire.london_clock(summer), (16, 30))
        self.assertEqual(empire.london_clock(summer_late), (19, 29))
        self.assertEqual(empire.london_clock(summer_edge), (19, 30))
        self.assertTrue(empire.is_evening(opening))
        self.assertTrue(empire.is_evening(middle))
        self.assertTrue(empire.is_evening(late))
        self.assertTrue(empire.is_evening(summer))
        self.assertTrue(empire.is_evening(summer_late))
        self.assertFalse(empire.is_evening(before))
        self.assertFalse(empire.is_evening(edge))
        self.assertFalse(empire.is_evening(summer_edge))
        self.assertTrue(empire.is_day(before))

        def claim(account: str, stamp: float) -> tuple[str, tuple[float, float], tuple[float, float], int]:
            engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            with Session() as session:
                empire.ensure_game(account, stamp, session)
                empire.tick_all(stamp + 1, session)
                rows = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
                car = next(row for row in rows if row.sign == "T1")
                people = len(session.scalars(select(empire.EmpirePerson).where(empire.EmpirePerson.username == account)).all())
                self.assertEqual(len(rows), 10, account)
                return car.status, (car.fare_lat, car.fare_lng), (car.drop_lat, car.drop_lng), people

        for stamp, account in (
            (opening, name),
            (middle, "evemid"),
            (late, "evelate"),
            (summer, "evesummer"),
            (summer_late, "evebst"),
        ):
            status, pickup, drop, people = claim(account, stamp)
            drawn = _draws_nearest_depot(account, stamp + 1, people)[0]
            self.assertEqual(status, "to_passenger", account)
            self.assertGreater(people, 10, account)
            self.assertEqual((pickup, drop), drawn[1:], account)
            self.assertTrue(empire.inside_circle(pickup), account)
            self.assertTrue(empire.inside_circle(drop), account)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_night_journey_is_quieter_and_the_airport_stays_uncommon(self) -> None:
        pickups = {empire.place_point(name) for name in empire.NIGHT_PICKUPS}
        homes = {empire.place_point(name) for name in empire.HOMES}
        airport = empire.place_point(empire.AIRPORT_NAME)
        town_or_coast = {empire.place_point(name) for name in empire.TOWN + empire.COAST}
        self.assertTrue(set(empire.NIGHT_PICKUPS).isdisjoint(empire.HOMES))
        name = None
        for index in range(80):
            candidate = f"nite{index}"
            if empire.night_ends(candidate) != empire.journey_ends(candidate):
                name = candidate
                break
        self.assertIsNotNone(name)
        late = datetime(2023, 11, 15, 22, 0, tzinfo=timezone.utc).timestamp()
        opening = datetime(2023, 11, 15, 19, 30, tzinfo=timezone.utc).timestamp()
        after_midnight = datetime(2023, 11, 15, 0, 30, tzinfo=timezone.utc).timestamp()
        summer = datetime(2024, 7, 15, 21, 0, tzinfo=timezone.utc).timestamp()
        before = datetime(2023, 11, 15, 19, 29, tzinfo=timezone.utc).timestamp()
        edge = datetime(2023, 11, 15, 1, 0, tzinfo=timezone.utc).timestamp()
        noon = datetime(2023, 11, 15, 12, 0, tzinfo=timezone.utc).timestamp()
        morning = datetime(2023, 11, 15, 8, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(empire.london_clock(summer), (22, 0))
        self.assertTrue(empire.is_night(late))
        self.assertTrue(empire.is_night(opening))
        self.assertTrue(empire.is_night(after_midnight))
        self.assertTrue(empire.is_night(summer))
        self.assertFalse(empire.is_night(before))
        self.assertFalse(empire.is_night(edge))
        self.assertTrue(empire.is_evening(before))
        self.assertFalse(empire.airport_sign("T1"))
        self.assertTrue(empire.airport_sign("T10"))

        def fleet(account: str, stamp: float, tick: float) -> dict[str, tuple[str, tuple[float, float], tuple[float, float]]]:
            engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            with Session() as session:
                empire.ensure_game(account, stamp, session)
                empire.tick_all(tick, session)
                rows = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
                self.assertEqual({car.sign for car in rows}, {f"T{number}" for number in range(1, 11)})
                return {
                    car.sign: (car.status, (car.fare_lat, car.fare_lng), (car.drop_lat, car.drop_lng))
                    for car in rows
                }

        for stamp, account in (
            (late, name),
            (opening, "niteopen"),
            (after_midnight, "nitemid"),
            (summer, "nitesummer"),
        ):
            cars = fleet(account, stamp, stamp + 64)
            moving = [sign for sign, (status, _pickup, _drop) in cars.items() if status == "to_passenger"]
            drawn = empire.draw_journey(account, stamp + 64, "person-0")
            self.assertEqual(moving, ["T1"], account)
            self.assertEqual(cars["T1"][1:], drawn[1:], account)
            self.assertTrue(empire.inside_circle(cars["T1"][1]), account)
            self.assertTrue(empire.inside_circle(cars["T1"][2]), account)
            for sign, (status, pickup, _drop) in cars.items():
                if sign == "T1":
                    continue
                self.assertNotEqual(status, "to_passenger", (account, sign))
                self.assertEqual(pickup, (0.0, 0.0), (account, sign))
        day_cars = fleet("niteday", noon, noon + 64)
        day_moving = [sign for sign, (status, _p, _d) in day_cars.items() if status == "to_passenger"]
        self.assertGreaterEqual(len(day_moving), 1)
        self.assertLess(len(day_moving), 10)
        self.assertEqual(day_cars["T1"][1:], empire.draw_journey("niteday", noon + 64, "person-0")[1:])
        morning_cars = fleet("nitemorn", morning, morning + 64)
        morning_draws = _draws_nearest_depot(
            "nitemorn", morning + 64, empire.spell_target("nitemorn", morning + 64)
        )
        self.assertEqual(morning_cars["T1"][0], "to_passenger")
        self.assertEqual(morning_cars["T1"][1:], morning_draws[0][1:])
        self.assertEqual(morning_cars["T2"][1:], morning_draws[1][1:])
        previous = fleet("niteprev", before, before + 1)["T1"]
        previous_draw = _draws_nearest_depot(
            "niteprev", before + 1, empire.spell_target("niteprev", before + 1)
        )[0]
        self.assertEqual(previous[0], "to_passenger")
        self.assertEqual(previous[1:], previous_draw[1:])
        edge_cars = fleet("niteone", edge, edge + 1)
        if empire.spell_target("niteone", edge + 1) == 0:
            self.assertEqual(edge_cars["T1"][1], (0.0, 0.0))
        else:
            self.assertEqual(edge_cars["T1"][0], "to_passenger")
            self.assertEqual(edge_cars["T1"][1:], empire.draw_journey("niteone", edge + 1, "person-0")[1:])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_drop_is_the_other_end_and_not_one_depot_bearing(self) -> None:
        places = {(lat, lng) for _name, lat, lng in empire.PLACES}
        ring_pickup, ring_drop = _ring("ada")
        self.assertLess(_bearing_gap(_depot_bearing(ring_pickup), _depot_bearing(ring_drop)), 0.05)
        found: dict[int, str] = {}
        number = 0
        while len(found) < 360:
            candidate = f"end{number}"
            found.setdefault(empire.raw_bearing(candidate), candidate)
            number += 1
            self.assertLess(number, 20000)
        for name in found.values():
            ring = _ring(name)
            ends = (
                empire.morning_ends(name),
                empire.day_ends(name),
                empire.evening_ends(name),
                empire.night_ends(name),
                empire.journey_ends(name),
                empire.airport_ends(name),
            )
            for pickup, drop in ends:
                self.assertNotEqual(pickup, drop, name)
                self.assertGreater(_bearing_gap(_depot_bearing(pickup), _depot_bearing(drop)), 0.5, name)
                self.assertNotIn(pickup, ring, name)
                self.assertNotIn(drop, ring, name)
                self.assertIn(pickup, places, name)
                self.assertIn(drop, places, name)
                self.assertNotEqual(pickup, empire.BERWICK, name)
                self.assertNotEqual(drop, empire.BERWICK, name)
        hours = (
            (datetime(2023, 11, 15, 8, 0, tzinfo=timezone.utc).timestamp(), "morning"),
            (DAY, "day"),
            (datetime(2023, 11, 15, 17, 0, tzinfo=timezone.utc).timestamp(), "evening"),
            (NOW, "night"),
            (datetime(2023, 11, 15, 3, 0, tzinfo=timezone.utc).timestamp(), "journey"),
        )
        for stamp, label in hours:
            engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            with Session() as session:
                empire.ensure_game("otherend", stamp, session)
                empire.tick_all(stamp + 64, session)
                cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "otherend")).all())
                people = list(
                    session.scalars(
                        select(empire.EmpirePerson).where(empire.EmpirePerson.username == "otherend")
                    ).all()
                )
            self.assertEqual({car.sign for car in cars}, {f"T{sign}" for sign in range(1, 11)}, label)
            target = empire.spell_target("otherend", stamp + 64)
            self.assertEqual(len(people), target, label)
            ring = _ring("otherend")
            collectors = [car for car in cars if car.passenger]
            self.assertEqual(len(collectors), min(10, target), label)
            by_sign = {person.sign: person for person in people}
            seen = []
            for car in collectors:
                person = by_sign[car.sign]
                pickup = (car.fare_lat, car.fare_lng)
                drop = (car.drop_lat, car.drop_lng)
                self.assertEqual(pickup, (person.lat, person.lng), (label, car.sign))
                self.assertEqual(drop, (person.drop_lat, person.drop_lng), (label, car.sign))
                self.assertNotEqual(pickup, drop, (label, car.sign))
                self.assertNotEqual(pickup, ring[0], (label, car.sign))
                self.assertNotEqual(drop, ring[1], (label, car.sign))
                self.assertTrue(empire.inside_circle(pickup), (label, car.sign))
                self.assertTrue(empire.inside_circle(drop), (label, car.sign))
                seen.append(pickup)
            if target > 1:
                self.assertGreater(len(set(seen)), 1, label)
            for car in cars:
                if car.passenger:
                    continue
                self.assertEqual(car.fare_name, "", (label, car.sign))
                self.assertEqual((car.fare_lat, car.fare_lng), (0.0, 0.0), (label, car.sign))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_person_waits_inside_the_circle_with_no_taxi(self) -> None:
        places = {(lat, lng) for _name, lat, lng in empire.PLACES}
        morning = datetime(2023, 11, 15, 8, 0, tzinfo=timezone.utc).timestamp()
        evening = datetime(2023, 11, 15, 17, 0, tzinfo=timezone.utc).timestamp()
        quiet = datetime(2023, 11, 15, 3, 0, tzinfo=timezone.utc).timestamp()
        hours = (NOW, DAY, morning, evening, quiet)

        def opened(stamp: float, account: str):
            engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            return engine, Session, account, stamp

        for stamp in hours:
            _engine, Session, account, stamp = opened(stamp, "waitperson")
            with Session() as session:
                empire.ensure_game(account, stamp, session)
                before = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
                self.assertEqual({car.sign for car in before}, {f"T{number}" for number in range(1, 11)})
                empire.tick_all(stamp + 1, session)
                body = empire.snapshot(account, stamp + 1, session)
                target = empire.spell_target(account, stamp + 1)
                people = body["people"]
                self.assertEqual(len(people), target, stamp)
                self.assertEqual([car["sign"] for car in body["cars"]], [f"T{number}" for number in range(1, 11)])
                self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
                self.assertTrue(all(fare["sign"] for fare in body["fares"]))
                self.assertEqual(body["fare_in"], 0)
                self.assertNotEqual(body["fare_in"], 800000)
                self.assertEqual(body["new_car"], 800000)
                moving = next(car for car in body["cars"] if car["sign"] == "T1")
                stored = session.scalars(
                    select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
                ).one()
                started = stored.leg_started
                route = stored.route_json
                point = None
                if people:
                    self.assertEqual(moving["status"], "to_passenger", stamp)
                    self.assertGreater(len(route), 2, stamp)
                    first = people[0]
                    _kind, drawn, drop = empire.draw_journey(account, stamp + 1, "person-0")
                    self.assertEqual(first["name"], "Mina", stamp)
                    self.assertEqual((first["lat"], first["lng"]), drawn, stamp)
                    person = min(
                        enumerate(people),
                        key=lambda item: (
                            empire.haversine_km(empire.DEPOT, (item[1]["lat"], item[1]["lng"])),
                            item[0],
                        ),
                    )[1]
                    point = (person["lat"], person["lng"])
                    self.assertEqual(person["sign"], "T1", stamp)
                    self.assertNotEqual(drawn, drop, stamp)
                    self.assertIn(point, places, stamp)
                    self.assertNotEqual(point, empire.BERWICK, stamp)
                    self.assertNotIn(point, _ring(account), stamp)
                    self.assertTrue(empire.inside_circle(point), stamp)
                    self.assertTrue(empire.inside_circle(drop), stamp)
                    self.assertGreater(empire.haversine_km(point, empire.BERWICK), 40, stamp)
                    self.assertEqual((moving["route"][-1][0], moving["route"][-1][1]), point, stamp)
                empire.tick_all(stamp + 2, session)
                again = empire.snapshot(account, stamp + 2, session)
                self.assertEqual(len(again["people"]), target, stamp)
                if people:
                    again_person = next(
                        person for person in again["people"] if (person["lat"], person["lng"]) == point
                    )
                    self.assertEqual(again_person["sign"], "T1")
                    self.assertEqual((again_person["lat"], again_person["lng"]), point)
                self.assertEqual([car["sign"] for car in again["cars"]], [f"T{number}" for number in range(1, 11)])
                stayed = session.scalars(
                    select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
                ).one()
                self.assertEqual(stayed.leg_started, started)
                self.assertEqual(stayed.route_json, route)
        _engine, Session, _account, stamp = opened(NOW, "waitperson")
        with Session() as session:
            empire.ensure_game("waitperson", NOW, session)
            empire.ensure_game("waitother", NOW, session)
            empire.tick_all(NOW + 1, session)
            one = empire.snapshot("waitperson", NOW + 1, session)
            other = empire.snapshot("waitother", NOW + 1, session)
            self.assertEqual(len(one["people"]), 1)
            self.assertEqual(len(other["people"]), 1)
            self.assertEqual(one["people"][0]["sign"], "T1")
            self.assertEqual(other["people"][0]["sign"], "T1")
            _kind, one_pickup, _one_drop = empire.draw_journey("waitperson", NOW + 1, "person-0")
            _kind, other_pickup, _other_drop = empire.draw_journey("waitother", NOW + 1, "person-0")
            self.assertEqual((one["people"][0]["lat"], one["people"][0]["lng"]), one_pickup)
            self.assertEqual((other["people"][0]["lat"], other["people"][0]["lng"]), other_pickup)
            self.assertEqual([car["sign"] for car in one["cars"]], [f"T{number}" for number in range(1, 11)])
            self.assertEqual([car["sign"] for car in other["cars"]], [f"T{number}" for number in range(1, 11)])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_tick_does_not_add_a_car_because_a_person_appeared(self) -> None:
        opening = [f"T{number}" for number in range(1, 11)]
        with self.Session() as session:
            empire.ensure_game("stayten", NOW, session)
            empire.ensure_game("staytwo", NOW, session)
            before = empire.snapshot("stayten", NOW, session)
            other_before = empire.snapshot("staytwo", NOW, session)
            self.assertEqual(before["people"], [])
            self.assertEqual(other_before["people"], [])
            self.assertEqual([car["sign"] for car in before["cars"]], opening)
            self.assertEqual([car["sign"] for car in other_before["cars"]], opening)
            empire.tick_all(NOW + 1, session)
            after = empire.snapshot("stayten", NOW + 1, session)
            other = empire.snapshot("staytwo", NOW + 1, session)
            self.assertEqual(len(after["people"]), 1)
            self.assertEqual(len(other["people"]), 1)
            self.assertEqual(after["people"][0]["sign"], "T1")
            self.assertEqual(other["people"][0]["sign"], "T1")
            self.assertEqual([car["sign"] for car in after["cars"]], opening)
            self.assertEqual([car["sign"] for car in other["cars"]], opening)
            self.assertNotIn("T11", [car["sign"] for car in after["cars"]])
            self.assertNotIn("T12", [car["sign"] for car in after["cars"]])
            empire.tick_all(NOW + 2, session)
            later = empire.snapshot("stayten", NOW + 2, session)
            self.assertEqual(len(later["people"]), 1)
            self.assertEqual([car["sign"] for car in later["cars"]], opening)
        with self.Session() as session:
            empire.ensure_game("staybuy", NOW, session)
            self.assertEqual(empire.buy_car("staybuy", 800000, NOW, session), "T11")
            bought = empire.snapshot("staybuy", NOW, session)
            self.assertEqual(bought["people"], [])
            self.assertEqual([car["sign"] for car in bought["cars"]], opening + ["T11"])
            empire.tick_all(NOW + 1, session)
            after_buy = empire.snapshot("staybuy", NOW + 1, session)
            self.assertEqual(len(after_buy["people"]), 1)
            self.assertEqual(after_buy["people"][0]["sign"], "T1")
            self.assertEqual([car["sign"] for car in after_buy["cars"]], opening + ["T11"])
            self.assertNotIn("T12", [car["sign"] for car in after_buy["cars"]])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_one_free_taxi_is_allocated_and_a_moving_car_keeps_its_route(self) -> None:
        with self.Session() as session:
            empire.ensure_game("freetaxi", NOW, session)
            parked = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "freetaxi", EmpireCar.sign == "T1")
            ).one()
            self.assertEqual(parked.status, "at_depot")
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("freetaxi", NOW + 1, session)
            self.assertEqual(len(body["people"]), 1)
            self.assertEqual(body["people"][0]["sign"], "T1")
            self.assertEqual([car["sign"] for car in body["cars"]], [f"T{number}" for number in range(1, 11)])
            moving = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "freetaxi", EmpireCar.sign == "T1")
            ).one()
            free = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "freetaxi", EmpireCar.sign == "T2")
            ).one()
            self.assertEqual(moving.status, "to_passenger")
            self.assertEqual(moving.leg_started, NOW + 1)
            self.assertGreater(len(moving.route_json), 2)
            self.assertEqual(free.status, "at_depot")
            self.assertEqual(free.leg_started, NOW)
            self.assertEqual(free.route_json, "[]")
            started = moving.leg_started
            route = moving.route_json
            free_started = free.leg_started
            free_route = free.route_json
            point = (body["people"][0]["lat"], body["people"][0]["lng"])
            empire.tick_all(NOW + 2, session)
            again = empire.snapshot("freetaxi", NOW + 2, session)
            self.assertEqual(len(again["people"]), 1)
            self.assertEqual(again["people"][0]["sign"], "T1")
            self.assertEqual((again["people"][0]["lat"], again["people"][0]["lng"]), point)
            stayed = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "freetaxi", EmpireCar.sign == "T1")
            ).one()
            still_free = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "freetaxi", EmpireCar.sign == "T2")
            ).one()
            self.assertEqual(stayed.leg_started, started)
            self.assertEqual(stayed.route_json, route)
            self.assertEqual(still_free.status, "at_depot")
            self.assertEqual(still_free.leg_started, free_started)
            self.assertEqual(still_free.route_json, free_route)
            self.assertEqual([car["sign"] for car in again["cars"]], [f"T{number}" for number in range(1, 11)])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_an_empty_move_goes_to_a_better_spot_and_does_not_replace_a_leg(self) -> None:
        places = {(lat, lng) for _name, lat, lng in empire.PLACES}
        _kind, drawn, drawn_drop = empire.draw_journey("emptymove", NOW + 64, "person-0")
        ring = _ring("emptymove")
        with self.Session() as session:
            empire.ensure_game("emptymove", NOW, session)
            empire.tick_all(NOW + 1, session)
            early = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "emptymove", EmpireCar.sign == "T2")
            ).one()
            self.assertEqual(early.status, "at_depot")
            self.assertEqual(early.route_json, "[]")
            started = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "emptymove", EmpireCar.sign == "T1")
            ).one()
            kept_route = started.route_json
            kept_started = started.leg_started
            self.assertEqual(started.status, "to_passenger")
            self.assertGreater(len(kept_route), 2)
            empire.tick_all(NOW + 64, session)
            body = empire.snapshot("emptymove", NOW + 64, session)
            self.assertEqual([car["sign"] for car in body["cars"]], [f"T{number}" for number in range(1, 11)])
            self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
            self.assertEqual(len(body["people"]), 1)
            self.assertEqual(body["people"][0]["sign"], "T1")
            self.assertEqual((body["people"][0]["lat"], body["people"][0]["lng"]), drawn)
            self.assertEqual(body["fare_in"], 0)
            self.assertNotEqual(body["fare_in"], 800000)
            self.assertEqual({fare["sign"] for fare in body["fares"]}, {"T1"})
            self.assertNotEqual(drawn, drawn_drop)
            mover = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "emptymove", EmpireCar.sign == "T2")
            ).one()
            other = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "emptymove", EmpireCar.sign == "T4")
            ).one()
            kept = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "emptymove", EmpireCar.sign == "T1")
            ).one()
            self.assertEqual(mover.passenger, "")
            self.assertEqual(mover.fare_name, "")
            self.assertEqual((mover.fare_lat, mover.fare_lng, mover.drop_lat, mover.drop_lng), (0.0, 0.0, 0.0, 0.0))
            self.assertNotEqual(mover.status, "with_passenger")
            spot = empire.strategic_spot("emptymove", "T2", NOW + 64)
            self.assertNotEqual(spot, drawn)
            if mover.status == "to_better_spot":
                route = empire._loads(mover.route_json, [])
                end = (route[-1][0], route[-1][1])
                self.assertEqual(end, spot)
                self.assertNotEqual(end, drawn)
                self.assertIn(end, places)
                self.assertNotEqual(end, empire.BERWICK)
                self.assertNotIn(end, ring)
                self.assertTrue(empire.inside_circle(end))
                self.assertAlmostEqual(route[0][0], empire.DEPOT[0], places=4)
                self.assertAlmostEqual(route[0][1], empire.DEPOT[1], places=4)
            else:
                self.assertEqual(mover.status, "at_depot")
                self.assertEqual(mover.job, "on break")
            self.assertEqual(other.passenger, "")
            self.assertEqual(other.fare_name, "")
            self.assertEqual((other.fare_lat, other.fare_lng, other.drop_lat, other.drop_lng), (0.0, 0.0, 0.0, 0.0))
            on_break = empire.car_takes_a_break("emptymove", "T4", NOW + 64, 1, 9)
            self.assertEqual(on_break, empire.car_takes_a_break("emptymove", "T4", NOW + 64, 1, 9))
            if on_break:
                self.assertEqual(other.status, "at_depot")
                self.assertEqual(other.job, "on break")
                self.assertAlmostEqual(other.hold_until - (NOW + 64), empire.BREAK_S, delta=1)
                self.assertLess(other.hold_until, NOW + 64 + 3600)
            else:
                self.assertEqual(other.status, "to_better_spot")
                self.assertEqual(other.job, "travelling to a better spot")
                other_route = empire._loads(other.route_json, [])
                self.assertEqual((other_route[-1][0], other_route[-1][1]), empire.strategic_spot("emptymove", "T4", NOW + 64))
                self.assertNotEqual((other_route[-1][0], other_route[-1][1]), drawn)
            self.assertEqual(kept.route_json, kept_route)
            self.assertEqual(kept.leg_started, kept_started)
            self.assertTrue(kept.passenger)
            mover_route = mover.route_json
            mover_started = mover.leg_started
            empire.tick_all(NOW + 65, session)
            still = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "emptymove", EmpireCar.sign == "T2")
            ).one()
            still_kept = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "emptymove", EmpireCar.sign == "T1")
            ).one()
            later = empire.snapshot("emptymove", NOW + 65, session)
            self.assertEqual(still.route_json, mover_route)
            self.assertEqual(still.leg_started, mover_started)
            self.assertEqual(still.passenger, "")
            self.assertEqual(still.fare_name, "")
            self.assertNotEqual(still.status, "with_passenger")
            self.assertEqual(still_kept.route_json, kept_route)
            self.assertEqual(still_kept.leg_started, kept_started)
            self.assertEqual(len(later["people"]), 1)
            self.assertEqual(later["people"][0]["sign"], "T1")
            self.assertEqual(later["fare_in"], 0)
            self.assertEqual([car["sign"] for car in later["cars"]], [f"T{number}" for number in range(1, 11)])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_the_car_stores_one_job_and_the_four_statuses_keep_working(self) -> None:
        self.assertEqual(
            empire.STATUSES,
            ("at_depot", "to_passenger", "with_passenger", "returning", "to_better_spot"),
        )
        self.assertEqual(
            empire.JOBS,
            ("waiting", "travelling to a better spot", "collecting", "carrying", "returning", "on break"),
        )
        with self.Session() as session:
            empire.ensure_game("onejob", NOW, session)
            parked = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "onejob", EmpireCar.sign == "T1")
            ).one()
            self.assertEqual(parked.status, "at_depot")
            self.assertEqual(parked.job, "waiting")
            empire.tick_all(NOW + 1, session)
            early = empire.snapshot("onejob", NOW + 1, session)
            self.assertEqual(self._car(early, "T1")["status"], "to_passenger")
            self.assertEqual(self._car(early, "T1")["job"], "collecting")
            self.assertEqual(self._car(early, "T2")["status"], "at_depot")
            self.assertEqual(self._car(early, "T2")["job"], "waiting")
            self.assertEqual([car["sign"] for car in early["cars"]], [f"T{number}" for number in range(1, 11)])
            started = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "onejob", EmpireCar.sign == "T1")
            ).one()
            kept_route = started.route_json
            kept_started = started.leg_started
            empire.tick_all(NOW + 8, session)
            body = empire.snapshot("onejob", NOW + 8, session)
            self.assertEqual(self._car(body, "T1")["status"], "to_passenger")
            self.assertEqual(self._car(body, "T1")["job"], "collecting")
            t2_break = empire.car_takes_a_break("onejob", "T2", NOW + 8, 1, 9)
            if t2_break:
                self.assertEqual(self._car(body, "T2")["status"], "at_depot")
                self.assertEqual(self._car(body, "T2")["job"], "on break")
            else:
                self.assertEqual(self._car(body, "T2")["status"], "to_better_spot")
                self.assertEqual(self._car(body, "T2")["job"], "travelling to a better spot")
            self.assertEqual(self._car(body, "T2")["passenger"], "")
            self.assertNotIn("T2", {fare["sign"] for fare in body["fares"]})
            self.assertEqual(body["fare_in"], 0)
            self.assertNotEqual(body["fare_in"], 800000)
            self.assertEqual(len(body["cars"]), 10)
            self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
            kept = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "onejob", EmpireCar.sign == "T1")
            ).one()
            self.assertEqual(kept.route_json, kept_route)
            self.assertEqual(kept.leg_started, kept_started)
            self.assertEqual(kept.job, "collecting")
            idle = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "onejob", EmpireCar.sign == "T2")
            ).one()
            if t2_break:
                self.assertEqual(idle.job, "on break")
            else:
                self.assertEqual(idle.job, "travelling to a better spot")
            sample = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "onejob", EmpireCar.sign == "T3")
            ).one()
            sample.status = "with_passenger"
            session.commit()
            carrying = empire.snapshot("onejob", NOW + 8, session)
            self.assertEqual(self._car(carrying, "T3")["status"], "with_passenger")
            self.assertEqual(self._car(carrying, "T3")["job"], "carrying")
            sample.status = "returning"
            session.commit()
            back = empire.snapshot("onejob", NOW + 8, session)
            self.assertEqual(self._car(back, "T3")["status"], "returning")
            self.assertEqual(self._car(back, "T3")["job"], "returning")
            sample.status = "at_depot"
            sample.leg_started = NOW + 8
            sample.hold_until = NOW + 8 + empire.DEPOT_PAUSE_S
            session.commit()
            home = empire.snapshot("onejob", NOW + 8, session)
            self.assertEqual(self._car(home, "T3")["status"], "at_depot")
            self.assertEqual(self._car(home, "T3")["job"], "waiting")
            self.assertEqual(self._car(home, "T1")["status"], "to_passenger")
            if t2_break:
                self.assertEqual(self._car(home, "T2")["status"], "at_depot")
            else:
                self.assertEqual(self._car(home, "T2")["status"], "to_better_spot")
            self.assertEqual([car["sign"] for car in home["cars"]], [f"T{number}" for number in range(1, 11)])
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_the_allocated_taxi_runs_the_passenger_trip_when_it_arrives(self) -> None:
        _kind, pickup, drop = empire.draw_journey("arrivedtrip", NOW + 1, "person-0")
        self.assertNotEqual(pickup, drop)
        self.assertTrue(empire.inside_circle(pickup))
        self.assertTrue(empire.inside_circle(drop))
        with self.Session() as session:
            empire.ensure_game("arrivedtrip", NOW, session)
            empire.tick_all(NOW + 1, session)
            early = empire.snapshot("arrivedtrip", NOW + 1, session)
            self.assertEqual(early["people"][0]["sign"], "T1")
            self.assertEqual(self._car(early, "T1")["status"], "to_passenger")
            self.assertEqual(self._car(early, "T1")["job"], "collecting")
            self.assertEqual(self._car(early, "T2")["status"], "at_depot")
            self.assertEqual(early["fare_in"], 0)
            moving = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "arrivedtrip", EmpireCar.sign == "T1")
            ).one()
            spare = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "arrivedtrip", EmpireCar.sign == "T4")
            ).one()
            self.assertEqual(moving.passenger, early["people"][0]["name"])
            self.assertEqual(spare.passenger, "")
            self.assertNotEqual(spare.status, "with_passenger")
            arrive = moving.leg_started + moving.duration_s + 1
            empire.tick_all(arrive, session)
            body = empire.snapshot("arrivedtrip", arrive, session)
            arrived = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "arrivedtrip", EmpireCar.sign == "T1")
            ).one()
            idle = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "arrivedtrip", EmpireCar.sign == "T4")
            ).one()
            self.assertEqual([car["sign"] for car in body["cars"]], [f"T{number}" for number in range(1, 11)])
            self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
            self.assertEqual(len(body["people"]), 1)
            self.assertNotEqual(body["people"][0]["sign"], "T1")
            appeared = empire.draw_journey("arrivedtrip", arrive, "person-1")
            self.assertEqual((body["people"][0]["lat"], body["people"][0]["lng"]), appeared[1])
            self.assertEqual(body["people"][0]["name"], "Owen")
            self.assertNotEqual((body["people"][0]["lat"], body["people"][0]["lng"]), pickup)
            self.assertEqual(arrived.status, "with_passenger")
            self.assertEqual(arrived.job, "carrying")
            self.assertEqual(arrived.passenger, early["people"][0]["name"])
            self.assertTrue(arrived.passenger)
            self.assertEqual(arrived.fare_name, "")
            self.assertEqual((arrived.fare_lat, arrived.fare_lng), pickup)
            self.assertEqual((arrived.drop_lat, arrived.drop_lng), drop)
            self.assertNotEqual((arrived.fare_lat, arrived.fare_lng), (arrived.drop_lat, arrived.drop_lng))
            self.assertTrue(empire.inside_circle((arrived.drop_lat, arrived.drop_lng)))
            self.assertTrue(empire.inside_circle((arrived.lat, arrived.lng)))
            self.assertNotIn("T1", {fare["sign"] for fare in body["fares"]})
            self.assertEqual(body["fare_in"], 0)
            self.assertNotEqual(body["fare_in"], 800000)
            self.assertEqual(body["new_car"], 800000)
            self.assertEqual(idle.passenger, "")
            self.assertEqual(idle.fare_name, "")
            self.assertNotEqual(idle.status, "with_passenger")
            self.assertNotEqual(idle.job, "carrying")
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_waiting_person_stays_until_their_taxi_arrives(self) -> None:
        morning = datetime(2023, 11, 15, 8, 0, tzinfo=timezone.utc).timestamp()

        def quick(start, end):
            return {"points": [[start[0], start[1]], [end[0], end[1]]], "duration_s": 30}

        empire.set_route_fn(quick)
        account = "staypin"
        with self.Session() as session:
            empire.ensure_game(account, morning, session)
            empire.tick_all(morning + 1, session)
            early = empire.snapshot(account, morning + 1, session)
            self.assertGreater(len(early["people"]), 10)
            self.assertEqual(len(early["cars"]), 10)
            carried = next(person for person in early["people"] if person["sign"] == "T1")
            moving = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
            ).one()
            self.assertEqual(moving.status, "to_passenger")
            arrive = moving.leg_started + moving.duration_s + 1
            empire.tick_all(arrive, session)
            body = empire.snapshot(account, arrive, session)
            car = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
            ).one()
        self.assertEqual(car.status, "with_passenger")
        self.assertEqual(car.passenger, carried["name"])
        self.assertEqual(len(body["cars"]), 10)
        self.assertNotIn("T11", [row["sign"] for row in body["cars"]])
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        carried_key = (carried["name"], carried["lat"], carried["lng"])
        before = [(person["name"], person["lat"], person["lng"]) for person in early["people"]]
        after = [(person["name"], person["lat"], person["lng"]) for person in body["people"]]
        self.assertEqual(before.count(carried_key), 1)
        self.assertEqual(after.count(carried_key), 0)
        firm_signs = {car["sign"] for firm in early["firms"] for car in firm["cars"]}
        for key in set(before):
            if key == carried_key:
                continue
            if after.count(key) >= before.count(key):
                continue
            self.assertTrue(
                any(
                    (person["name"], person["lat"], person["lng"]) == key and person["sign"] in firm_signs
                    for person in early["people"]
                )
            )
        appeared = empire.draw_journey(account, arrive, f"person-{len(early['people'])}")
        self.assertIn((empire.NAMES[len(early["people"]) % len(empire.NAMES)], appeared[1][0], appeared[1][1]), after)
        self.assertEqual(len(after), len(before))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_each_taxi_draws_its_own_spot(self) -> None:
        places = {(lat, lng) for _name, lat, lng in empire.PLACES}
        now = DAY + 64
        account = "apart56"
        first = empire.strategic_spot(account, "T9", now)
        shared = empire.strategic_spot(account, "T10", now)
        self.assertEqual(first, shared)
        self.assertEqual(first, empire.strategic_spot(account, "T9", now))
        own = empire.strategic_spot(account, "T10", now, (first,))
        self.assertNotEqual(own, first)
        self.assertEqual(own, empire.strategic_spot(account, "T10", now, (first,)))
        self.assertIn(own, places)
        self.assertTrue(empire.inside_circle(own))
        self.assertEqual(empire.spell_target(account, now), 1)
        self.assertFalse(empire.car_takes_a_break(account, "T9", now, 1, 10))
        self.assertFalse(empire.car_takes_a_break(account, "T10", now, 1, 10))
        ring = _ring(account)
        with self.Session() as session:
            empire.ensure_game(account, DAY, session)
            held = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T3")
            ).one()
            held.status = "returning"
            held.passenger = ""
            held.fare_name = ""
            held.route_json = "[[55.01, -1.51], [55.02, -1.52]]"
            held.duration_s = 10000
            held.leg_started = now
            held.hold_until = DAY
            session.commit()
            kept_route = held.route_json
            empire.tick_all(now, session)
            body = empire.snapshot(account, now, session)
            cars = {
                car.sign: car
                for car in session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all()
            }
            still = cars["T3"]
            self.assertEqual(still.status, "returning")
            self.assertEqual(still.route_json, kept_route)
            self.assertEqual(still.leg_started, now)
            empire.tick_all(now + 1, session)
            again = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T3")
            ).one()
            kept_fare = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
            ).one()
            fare_route = kept_fare.route_json
            fare_started = kept_fare.leg_started
            self.assertEqual(again.status, "returning")
            self.assertEqual(again.route_json, kept_route)
            empire.tick_all(now + 2, session)
            fare_still = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
            ).one()
        self.assertEqual(fare_still.route_json, fare_route)
        self.assertEqual(fare_still.leg_started, fare_started)
        self.assertEqual(fare_still.status, "to_passenger")
        self.assertEqual(len(body["cars"]), 10)
        self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertEqual(body["new_car"], 800000)
        self.assertEqual(len(body["people"]), 1)
        self.assertEqual(body["people"][0]["sign"], "T1")
        nine = cars["T9"]
        ten = cars["T10"]
        self.assertEqual(nine.status, "to_better_spot")
        self.assertEqual(ten.status, "to_better_spot")
        nine_route = empire._loads(nine.route_json, [])
        ten_route = empire._loads(ten.route_json, [])
        nine_end = (nine_route[-1][0], nine_route[-1][1])
        ten_end = (ten_route[-1][0], ten_route[-1][1])
        self.assertEqual(nine_end, first)
        self.assertEqual(ten_end, own)
        self.assertNotEqual(nine_end, ten_end)
        self.assertNotEqual(nine_route, ten_route)
        self.assertNotEqual(nine_end, (body["people"][0]["lat"], body["people"][0]["lng"]))
        self.assertNotEqual(ten_end, (body["people"][0]["lat"], body["people"][0]["lng"]))
        for end in (nine_end, ten_end):
            self.assertIn(end, places)
            self.assertTrue(empire.inside_circle(end))
            self.assertNotIn(end, ring)
            self.assertNotEqual(end, empire.DEPOT)
        self.assertEqual(nine.passenger, "")
        self.assertEqual(ten.passenger, "")
        self.assertEqual(nine.fare_name, "")
        self.assertEqual(ten.fare_name, "")
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_the_engine_draws_the_person_the_journey_and_the_idle_taxi(self) -> None:
        places = {(lat, lng) for _name, lat, lng in empire.PLACES}
        morning = datetime(2023, 11, 15, 8, 0, tzinfo=timezone.utc).timestamp()
        quiet = datetime(2023, 11, 15, 3, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(
            empire.draw_journey("stable", morning, "person-0"),
            empire.draw_journey("stable", morning, "person-0"),
        )
        morning_kinds = {
            empire.draw_journey(f"mix{index}", morning, "person-0")[0] for index in range(40)
        }
        self.assertGreater(len(morning_kinds), 1)
        morning_commute = 0
        morning_night_out = 0
        night_commute = 0
        night_out = 0
        for index in range(400):
            kind, pickup, drop = empire.draw_journey(f"hour{index}", morning, "person-0")
            self.assertNotEqual(pickup, drop)
            self.assertIn(pickup, places)
            self.assertIn(drop, places)
            self.assertTrue(empire.inside_circle(pickup))
            self.assertTrue(empire.inside_circle(drop))
            self.assertGreater(empire.haversine_km(pickup, empire.BERWICK), 40)
            if kind == "commute":
                morning_commute += 1
            if kind == "night out":
                morning_night_out += 1
            kind_n, pickup_n, drop_n = empire.draw_journey(f"hour{index}", NOW, "person-0")
            self.assertNotEqual(pickup_n, drop_n)
            self.assertIn(pickup_n, places)
            self.assertIn(drop_n, places)
            if kind_n == "commute":
                night_commute += 1
            if kind_n == "night out":
                night_out += 1
        self.assertEqual(morning_night_out, 0)
        self.assertEqual(night_commute, 0)
        self.assertGreater(morning_commute, 150)
        self.assertGreater(night_out, 150)
        rng = random.Random(1)
        killingworth = 0
        earsdon = 0
        for _ in range(1000):
            name = empire._pick_name(rng, empire.HOMES)
            if name == "Killingworth":
                killingworth += 1
            if name == "Earsdon":
                earsdon += 1
        self.assertGreater(killingworth, earsdon)

        quiet_name = None
        for index in range(40):
            name = f"quiet{index}"
            if empire.spell_target(name, quiet) == 0:
                quiet_name = name
                break
        self.assertIsNotNone(quiet_name)
        with self.Session() as session:
            empire.ensure_game(quiet_name, quiet, session)
            empire.tick_all(quiet + 1, session)
            empty = empire.snapshot(quiet_name, quiet + 1, session)
            self.assertEqual(empty["people"], [])
            self.assertEqual([car["sign"] for car in empty["cars"]], [f"T{number}" for number in range(1, 11)])
            self.assertNotIn("T11", [car["sign"] for car in empty["cars"]])
            self.assertEqual(empty["fare_in"], 0)
            self.assertNotEqual(empty["fare_in"], 800000)
            self.assertEqual(empty["new_car"], 800000)

        with self.Session() as session:
            empire.ensure_game("busydraw", morning, session)
            for step in range(1, 17):
                empire.tick_all(morning + step, session)
            body = empire.snapshot("busydraw", morning + 16, session)
            free = sum(1 for car in body["cars"] if car["status"] == "at_depot")
            self.assertGreater(len(body["people"]), 10)
            self.assertGreater(len(body["people"]), free)
            self.assertEqual(len(body["cars"]), 10)
            self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
            self.assertEqual(body["fare_in"], 0)
            self.assertNotEqual(body["fare_in"], 800000)
            self.assertEqual(body["new_car"], 800000)
            for person in body["people"]:
                point = (person["lat"], person["lng"])
                self.assertIn(point, places)
                self.assertTrue(empire.inside_circle(point))
                self.assertNotEqual(point, empire.BERWICK)

        self.assertEqual(
            empire.car_takes_a_break("breakseed", "T4", NOW, 0, 4),
            empire.car_takes_a_break("breakseed", "T4", NOW, 0, 4),
        )
        stayed_out = 0
        sent_home = 0
        for index in range(80):
            if empire.car_takes_a_break(f"quietcar{index}", "T4", NOW, 0, 4):
                sent_home += 1
            else:
                stayed_out += 1
        self.assertGreater(sent_home, stayed_out)
        self.assertGreater(stayed_out, 0)
        busy_home = 0
        busy_out = 0
        for index in range(80):
            if empire.car_takes_a_break(f"busycar{index}", "T8", NOW, 12, 2):
                busy_home += 1
            else:
                busy_out += 1
        self.assertGreater(busy_out, busy_home)
        self.assertGreater(busy_home, 0)

        pickup = empire.place_point("Earsdon")
        near_depot = (55.0464, -1.4783)
        far_depot = (54.9783, -1.6178)
        near_wins = 0
        for index in range(200):
            chosen = empire.depot_takes_pickup(pickup, (near_depot, far_depot), f"terr{index}", "pick")
            if chosen == near_depot:
                near_wins += 1
        self.assertGreater(near_wins, 150)
        self.assertEqual(
            empire.depot_takes_pickup(pickup, (empire.DEPOT,), "onlydepot", "pick"),
            empire.DEPOT,
        )

        whitley = empire.place_point("Whitley Bay")
        with self.Session() as session:
            game = empire.ensure_game("legstart", DAY, session)
            car = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "legstart", EmpireCar.sign == "T1")
            ).one()
            car.lat, car.lng = whitley
            car.route_json = f"[[{whitley[0]}, {whitley[1]}], [{whitley[0]}, {whitley[1]}]]"
            car.status = "at_depot"
            empire._start_fare(car, game, DAY)
            started = empire._loads(car.route_json, [])
            self.assertAlmostEqual(started[0][0], whitley[0], places=4)
            self.assertAlmostEqual(started[0][1], whitley[1], places=4)
            self.assertGreater(empire.haversine_km((started[0][0], started[0][1]), empire.DEPOT), 0.2)
            self.assertEqual(len(session.scalars(select(EmpireCar).where(EmpireCar.username == "legstart")).all()), 10)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_the_feed_names_the_place_at_the_end_of_the_leg(self) -> None:
        names = {name for name, _lat, _lng in empire.PLACES}
        self.assertEqual(empire.place_name(empire.place_point("Benton")), "Benton")
        self.assertEqual(empire.place_name(empire.DEPOT), "Shiremoor")
        self.assertEqual(empire.place_name((0.0, 0.0)), "")
        account = "legplace"
        with self.Session() as session:
            empire.ensure_game(account, NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot(account, NOW + 1, session)
        home = self._car(body, "T2")
        moving = self._car(body, "T1")
        self.assertEqual(home["status"], "at_depot")
        self.assertEqual(home["destination"], "Shiremoor")
        end = (moving["route"][-1][0], moving["route"][-1][1])
        self.assertEqual(moving["destination"], empire.place_name(end))
        self.assertIn(moving["destination"], names)
        self.assertEqual(len(body["cars"]), 10)
        self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
        self.assertEqual(body["fare_in"], 0)
        self.assertEqual(body["new_car"], 800000)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_other_firms_take_fares_the_player_is_not_covering(self) -> None:
        morning = datetime(2023, 11, 15, 8, 0, tzinfo=timezone.utc).timestamp()
        account = "othfirm"
        later = morning + 94
        with self.Session() as session:
            before_devices = (
                session.scalar(select(func.count()).select_from(Device)),
                session.scalar(select(func.count()).select_from(LocationPing)),
                session.scalar(select(func.count()).select_from(DeviceLink)),
            )
            empire.ensure_game(account, morning, session)
            player_before = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
            self.assertEqual(len(player_before), 10)
            south = empire.firm_key(account, "south")
            held = session.scalars(
                select(EmpireCar).where(EmpireCar.username == south, EmpireCar.sign == "S3")
            ).one()
            held.status = "returning"
            held.passenger = ""
            held.fare_name = ""
            held.route_json = "[[55.01, -1.51], [55.02, -1.52]]"
            held.duration_s = 10000
            held.leg_started = morning + 64
            kept_route = held.route_json
            session.commit()
            empire.tick_all(morning + 64, session)
            body = empire.snapshot(account, later, session)
            after_devices = (
                session.scalar(select(func.count()).select_from(Device)),
                session.scalar(select(func.count()).select_from(LocationPing)),
                session.scalar(select(func.count()).select_from(DeviceLink)),
            )
            player = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
            kept = session.scalars(
                select(EmpireCar).where(EmpireCar.username == south, EmpireCar.sign == "S3")
            ).one()
            kept_status = kept.status
            kept_started = kept.leg_started
            people = empire._people(session, account)
            id_by_sign = {person.sign: person.id for person in people}
            firm_miles = 0.0
            for firm_name in empire.FIRM_NAMES:
                for car in session.scalars(
                    select(EmpireCar).where(EmpireCar.username == empire.firm_key(account, firm_name))
                ).all():
                    firm_miles += empire.driven_miles(car, later)
            player_miles = sum(empire.driven_miles(car, later) for car in player)
        self.assertEqual(before_devices, after_devices)
        self.assertEqual([car["sign"] for car in body["cars"]], [f"T{number}" for number in range(1, 11)])
        self.assertEqual(len(player), 10)
        self.assertNotIn("T11", [car.sign for car in player])
        self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertEqual(body["new_car"], 800000)
        self.assertEqual(body["cash_pence"], 0)
        self.assertEqual(body["standing"], empire.STANDING_PENCE * 10)
        self.assertEqual(body["driver_share"], 0)
        self.assertEqual(body["fuel"], empire.fuel_pence(player_miles))
        self.assertEqual(kept_status, "returning")
        self.assertEqual(kept_route, "[[55.01, -1.51], [55.02, -1.52]]")
        self.assertEqual(kept_started, morning + 64)
        homes = empire.firm_depots(account)
        self.assertEqual(homes, empire.firm_depots(account))
        self.assertEqual(len(body["firms"]), 2)
        self.assertEqual([firm["firm"] for firm in body["firms"]], list(empire.FIRM_NAMES))
        seen = set()
        firm_signs = set()
        for firm in body["firms"]:
            depot = (firm["depot"][0], firm["depot"][1])
            self.assertEqual(depot, homes[firm["firm"]])
            self.assertNotEqual(depot, empire.DEPOT)
            self.assertGreater(empire.haversine_km(depot, empire.place_point("Shiremoor")), 0.05)
            self.assertNotIn(depot, seen)
            seen.add(depot)
            self.assertEqual(len(firm["cars"]), 10)
            for car in firm["cars"]:
                firm_signs.add(car["sign"])
                self.assertNotIn(car["sign"], {f"T{number}" for number in range(1, 12)})
                if car["status"] == "at_depot":
                    self.assertAlmostEqual(car["lat"], depot[0], places=4)
                    self.assertAlmostEqual(car["lng"], depot[1], places=4)
        self.assertGreater(len(body["people"]), 10)
        claimed = [person for person in body["people"] if person["sign"] in firm_signs]
        self.assertGreaterEqual(len(claimed), 1)
        depots = tuple(homes[firm] for firm in empire.FIRM_NAMES)
        firm_by_sign = {car["sign"]: firm for firm in body["firms"] for car in firm["cars"]}
        for person in claimed:
            pickup = (person["lat"], person["lng"])
            winner = min(
                depots,
                key=lambda depot: (empire.haversine_km(depot, pickup), depot[0], depot[1]),
            )
            self.assertEqual([winner[0], winner[1]], firm_by_sign[person["sign"]]["depot"])
            car = next(item for item in firm_by_sign[person["sign"]]["cars"] if item["sign"] == person["sign"])
            self.assertEqual(car["status"], "to_passenger")
            self.assertEqual(car["job"], "collecting")
            self.assertEqual((car["route"][-1][0], car["route"][-1][1]), (person["lat"], person["lng"]))
        near = homes["north"]
        far = homes["south"]
        near_wins = 0
        for index in range(80):
            chosen = empire.depot_takes_pickup(near, (near, far), account, f"near-{index}")
            if chosen == near:
                near_wins += 1
        self.assertGreater(near_wins, 60)
        self.assertGreater(firm_miles, 0)
        self.assertGreater(player_miles, 0)
        self.assertNotEqual(body["fuel"], empire.fuel_pence(player_miles + firm_miles))
        self.assertNotIn("device_id", EmpireCar.__table__.columns.keys())
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_finished_car_takes_the_closest_person_from_where_it_is(self) -> None:
        night = datetime(2023, 11, 15, 22, 0, tzinfo=timezone.utc).timestamp()
        account = "nearcar"
        home = empire.place_point("Whitley Bay")
        drop = empire.place_point("Tynemouth")
        owen_at = empire.destination(drop, 40, 0.2)
        priya_at = empire.destination(home, 20, 0.15)
        self.assertGreater(empire.haversine_km(home, drop), 0.5)
        self.assertEqual(empire.spell_target(account, night), 1)
        with self.Session() as session:
            empire.ensure_game(account, night, session)
            held = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T3")
            ).one()
            held.status = "returning"
            held.passenger = ""
            held.fare_name = ""
            held.route_json = "[[55.01, -1.51], [55.02, -1.52]]"
            held.duration_s = 10000
            held.leg_started = night
            kept_route = held.route_json
            finished = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
            ).one()
            finished.status = "with_passenger"
            finished.passenger = "Mina"
            finished.fare_name = ""
            finished.fare_lat, finished.fare_lng = home
            finished.drop_lat, finished.drop_lng = drop
            finished.route_json = json.dumps([[home[0], home[1]], [drop[0], drop[1]]])
            earned = empire.north_tyneside_tariff_pence(
                empire.route_miles([[home[0], home[1]], [drop[0], drop[1]]]),
                30,
            )
            finished.duration_s = 30
            finished.leg_started = night
            finished.lat, finished.lng = home
            near = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T2")
            ).one()
            near.status = "at_depot"
            near.lat, near.lng = home
            near.route_json = json.dumps([[home[0], home[1]], [home[0], home[1]]])
            near.hold_until = night
            near.duration_s = 0
            session.add(
                empire.EmpirePerson(
                    username=account,
                    name="Owen",
                    lat=owen_at[0],
                    lng=owen_at[1],
                    sign="",
                    drop_lat=home[0],
                    drop_lng=home[1],
                    arrived=night - 40,
                )
            )
            session.add(
                empire.EmpirePerson(
                    username=account,
                    name="Priya",
                    lat=priya_at[0],
                    lng=priya_at[1],
                    sign="",
                    drop_lat=drop[0],
                    drop_lng=drop[1],
                    arrived=night - 40,
                )
            )
            session.commit()
            empire.tick_all(night + 30, session)
            body = empire.snapshot(account, night + 30, session)
            kept = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T3")
            ).one()
            player = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
        self.assertEqual(len(player), 10)
        self.assertNotIn("T11", [car.sign for car in player])
        self.assertEqual(kept.status, "returning")
        self.assertEqual(kept.route_json, kept_route)
        self.assertEqual(body["fare_in"], earned)
        self.assertNotEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertEqual(body["driver_share"], earned // 2)
        self.assertEqual(body["new_car"], 800000)
        collected = self._car(body, "T1")
        parked = self._car(body, "T2")
        self.assertEqual(collected["status"], "to_passenger")
        self.assertEqual(collected["job"], "collecting")
        self.assertAlmostEqual(collected["route"][0][0], drop[0], places=4)
        self.assertAlmostEqual(collected["route"][0][1], drop[1], places=4)
        self.assertAlmostEqual(collected["route"][-1][0], owen_at[0], places=4)
        self.assertAlmostEqual(collected["route"][-1][1], owen_at[1], places=4)
        self.assertNotEqual((round(collected["route"][0][0], 4), round(collected["route"][0][1], 4)), (round(empire.DEPOT[0], 4), round(empire.DEPOT[1], 4)))
        self.assertEqual(parked["status"], "to_passenger")
        self.assertAlmostEqual(parked["route"][0][0], home[0], places=4)
        self.assertAlmostEqual(parked["route"][-1][0], priya_at[0], places=4)
        self.assertGreater(collected["seconds_left"], 0)
        self.assertEqual(parked["seconds_left"], int(parked["duration_s"]))
        owen = next(person for person in body["people"] if person["name"] == "Owen")
        priya = next(person for person in body["people"] if person["name"] == "Priya")
        self.assertEqual(owen["sign"], "T1")
        self.assertEqual(priya["sign"], "T2")
        self.assertEqual(owen["seconds_left"], collected["seconds_left"])
        self.assertEqual(priya["seconds_left"], parked["seconds_left"])
        self.assertEqual(owen["waiting_s"], 70)
        self.assertEqual(priya["waiting_s"], 70)
        signs = [person["sign"] for person in body["people"] if person["sign"] in {"T1", "T2"}]
        self.assertEqual(len(signs), len(set(signs)))
        self.assertNotIn("device_id", empire.EmpirePerson.__table__.columns.keys())
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_firm_takes_the_closest_leftover_car(self) -> None:
        night = datetime(2023, 11, 15, 22, 0, tzinfo=timezone.utc).timestamp()
        account = "firmnear"
        with self.Session() as session:
            empire.ensure_game(account, night, session)
            homes = empire.firm_depots(account)
            jack_at = empire.destination(homes["north"], 15, 0.2)
            for car in session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all():
                car.status = "returning"
                car.passenger = ""
                car.fare_name = ""
                car.route_json = "[[55.01, -1.51], [55.02, -1.52]]"
                car.duration_s = 10000
                car.leg_started = night
            session.add(
                empire.EmpirePerson(
                    username=account,
                    name="Jack",
                    lat=jack_at[0],
                    lng=jack_at[1],
                    sign="",
                    drop_lat=homes["south"][0],
                    drop_lng=homes["south"][1],
                    arrived=night,
                )
            )
            session.commit()
            empire.tick_all(night + 1, session)
            body = empire.snapshot(account, night + 1, session)
            player = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
        self.assertEqual(len(player), 10)
        self.assertTrue(all(car.status == "returning" for car in player))
        jack = next(person for person in body["people"] if person["name"] == "Jack")
        self.assertTrue(jack["sign"].startswith("N"))
        north = next(firm for firm in body["firms"] if firm["firm"] == "north")
        car = next(item for item in north["cars"] if item["sign"] == jack["sign"])
        self.assertEqual(car["status"], "to_passenger")
        self.assertEqual(car["job"], "collecting")
        self.assertEqual(car["seconds_left"], jack["seconds_left"])
        self.assertGreater(car["seconds_left"], 0)
        self.assertAlmostEqual(car["route"][0][0], homes["north"][0], places=4)
        self.assertAlmostEqual(car["route"][-1][0], jack_at[0], places=4)
        self.assertEqual(jack["waiting_s"], 1)
        self.assertEqual(body["fare_in"], 0)
        self.assertEqual(len(body["cars"]), 10)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_suite_does_not_use_the_live_database_or_the_network(self) -> None:
        live = ROOT / "hackathon.db"
        before = live.stat().st_mtime if live.exists() else None
        self.assertFalse(empire.clock_running())
        self.assertTrue(TEMP_DB.endswith(".db"))
        after = live.stat().st_mtime if live.exists() else None
        self.assertEqual(before, after)

    def test_idle_taxis_of_that_firm_loiter_inside_the_patch(self) -> None:
        quiet = datetime(2023, 11, 15, 3, 0, tzinfo=timezone.utc).timestamp()
        centre, radius = _empty_patch()
        account = _quiet_account("patch", quiet + 64)
        self.assertEqual(empire.spell_target(account, quiet + 64), 0)
        first = empire.loiter_spot(account, "T9", quiet + 64, centre, radius)
        self.assertEqual(first, empire.loiter_spot(account, "T9", quiet + 64, centre, radius))
        other = empire.loiter_spot(account, "T10", quiet + 64, centre, radius, (first,))
        self.assertNotEqual(other, first)
        self.assertLessEqual(empire.haversine_km(centre, first), radius)
        self.assertLessEqual(empire.haversine_km(centre, other), radius)
        self.assertTrue(empire.inside_circle(first))
        with self.Session() as session:
            empire.ensure_game(account, quiet, session)
            game = session.get(EmpireGame, account)
            game.cash_pence = 40
            session.commit()
            empty = empire.snapshot(account, quiet, session)
            self.assertIsNone(empty["loiter"])
            self.assertFalse(empire.set_loiter(account, empire.BERWICK[0], empire.BERWICK[1], radius, "", quiet, session))
            self.assertFalse(empire.set_loiter(account, centre[0], centre[1], 0, "", quiet, session))
            self.assertFalse(empire.set_loiter(account, centre[0], centre[1], radius, "west", quiet, session))
            self.assertIsNone(empire.snapshot(account, quiet, session)["loiter"])
            self.assertTrue(empire.set_loiter(account, centre[0], centre[1], radius, "", quiet, session))
            empire.tick_all(quiet + 64, session)
            body = empire.snapshot(account, quiet + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
            north = list(
                session.scalars(
                    select(EmpireCar).where(EmpireCar.username == empire.firm_key(account, "north"))
                ).all()
            )
        self.assertEqual(body["loiter"]["firm"], "")
        self.assertAlmostEqual(body["loiter"]["lat"], centre[0], places=5)
        self.assertAlmostEqual(body["loiter"]["lng"], centre[1], places=5)
        self.assertEqual(body["loiter"]["radius_km"], radius)
        self.assertEqual(body["cash_pence"], 40)
        self.assertEqual(body["fare_in"], 0)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertEqual(body["new_car"], 800000)
        self.assertEqual(len(body["cars"]), 10)
        self.assertNotIn("T11", [car["sign"] for car in body["cars"]])
        self.assertEqual(body["people"], [])
        self.assertEqual(len(cars), 10)
        ends = []
        for car in cars:
            self.assertEqual(car.status, "to_better_spot", car.sign)
            self.assertEqual(car.passenger, "")
            self.assertEqual(car.fare_name, "")
            route = empire._loads(car.route_json, [])
            end = (route[-1][0], route[-1][1])
            self.assertLessEqual(empire.haversine_km(centre, end), radius, car.sign)
            self.assertTrue(empire.inside_circle(end))
            self.assertNotEqual(end, empire.DEPOT)
            ends.append((round(end[0], 5), round(end[1], 5)))
        self.assertEqual(len(ends), len(set(ends)))
        for car in north:
            route = empire._loads(car.route_json, [])
            if len(route) < 2:
                continue
            end = (float(route[-1][0]), float(route[-1][1]))
            self.assertGreater(empire.haversine_km(centre, end), radius, car.sign)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_car_on_a_leg_keeps_it_when_the_patch_is_set(self) -> None:
        centre, radius = _empty_patch()
        account = "patchkeep"
        stamp = DAY + 64
        with self.Session() as session:
            empire.ensure_game(account, DAY, session)
            self.assertTrue(empire.set_loiter(account, centre[0], centre[1], radius, "", DAY, session))
            kept = {}
            for sign, status in (("T3", "returning"), ("T4", "with_passenger"), ("T5", "to_passenger")):
                car = session.scalars(
                    select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == sign)
                ).one()
                car.status = status
                car.passenger = "Mina" if status == "with_passenger" else ""
                car.fare_name = "Mina" if status == "to_passenger" else ""
                car.route_json = json.dumps([[55.01, -1.50], [55.02, -1.49]])
                car.duration_s = 10000
                car.leg_started = stamp
                car.hold_until = DAY
                kept[sign] = (car.status, car.route_json, car.leg_started)
            session.commit()
            empire.tick_all(stamp, session)
            body = empire.snapshot(account, stamp, session)
            for sign, (status, route_json, started) in kept.items():
                car = session.scalars(
                    select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == sign)
                ).one()
                self.assertEqual(car.status, status, sign)
                self.assertEqual(car.route_json, route_json, sign)
                self.assertEqual(car.leg_started, started, sign)
            idle = [
                car
                for car in session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all()
                if car.sign not in kept and car.status == "to_better_spot"
            ]
        self.assertGreaterEqual(len(idle), 2)
        ends = []
        for car in idle:
            route = empire._loads(car.route_json, [])
            end = (route[-1][0], route[-1][1])
            self.assertLessEqual(empire.haversine_km(centre, end), radius, car.sign)
            ends.append((round(end[0], 5), round(end[1], 5)))
        self.assertEqual(len(ends), len(set(ends)))
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(body["fare_in"], 0)
        self.assertEqual(body["cash_pence"], 0)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_north_patch_does_not_move_the_players_cars(self) -> None:
        quiet = datetime(2023, 11, 15, 3, 0, tzinfo=timezone.utc).timestamp()
        centre, radius = _empty_patch()
        account = _quiet_account("nfirm", quiet + 64)
        with self.Session() as session:
            empire.ensure_game(account, quiet, session)
            self.assertTrue(
                empire.set_loiter(account, centre[0], centre[1], radius, "north", quiet, session)
            )
            empire.tick_all(quiet + 64, session)
            body = empire.snapshot(account, quiet + 64, session)
            player = list(session.scalars(select(EmpireCar).where(EmpireCar.username == account)).all())
            north = list(
                session.scalars(
                    select(EmpireCar).where(EmpireCar.username == empire.firm_key(account, "north"))
                ).all()
            )
            south = list(
                session.scalars(
                    select(EmpireCar).where(EmpireCar.username == empire.firm_key(account, "south"))
                ).all()
            )
        self.assertEqual(body["loiter"]["firm"], "north")
        self.assertEqual(body["fare_in"], 0)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(len(north), 10)
        north_ends = []
        for car in north:
            self.assertEqual(car.status, "to_better_spot", car.sign)
            route = empire._loads(car.route_json, [])
            end = (route[-1][0], route[-1][1])
            self.assertLessEqual(empire.haversine_km(centre, end), radius, car.sign)
            north_ends.append((round(end[0], 5), round(end[1], 5)))
        self.assertEqual(len(north_ends), len(set(north_ends)))
        for car in player + south:
            route = empire._loads(car.route_json, [])
            if len(route) < 2:
                point = (float(car.lat), float(car.lng))
            else:
                point = (float(route[-1][0]), float(route[-1][1]))
            self.assertGreater(empire.haversine_km(centre, point), radius, car.sign)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_two_idle_taxis_already_inside_the_patch_do_not_share_a_point(self) -> None:
        quiet = datetime(2023, 11, 15, 3, 0, tzinfo=timezone.utc).timestamp()
        centre, radius = _empty_patch()
        account = _quiet_account("stay", quiet + 64)
        sat = empire.destination(centre, 15, 0.1)
        self.assertLessEqual(empire.haversine_km(centre, sat), radius)
        stamp = quiet + 64
        with self.Session() as session:
            empire.ensure_game(account, quiet, session)
            self.assertTrue(empire.set_loiter(account, centre[0], centre[1], radius, "", quiet, session))
            home = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
            ).one()
            home.status = "at_depot"
            home.lat, home.lng = sat
            home.route_json = json.dumps([[sat[0], sat[1]], [sat[0], sat[1]]])
            home.duration_s = 0
            home.leg_started = quiet
            home.hold_until = quiet
            home.job = "waiting"
            mover = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T2")
            ).one()
            mover.hold_until = quiet
            for sign in [f"T{number}" for number in range(3, 11)]:
                car = session.scalars(
                    select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == sign)
                ).one()
                car.status = "returning"
                car.passenger = ""
                car.fare_name = ""
                car.route_json = json.dumps([[55.04, -1.60], [55.05, -1.61]])
                car.duration_s = 10000
                car.leg_started = stamp
            session.commit()
            kept = home.route_json
            empire.tick_all(stamp, session)
            still = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T1")
            ).one()
            moved = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T2")
            ).one()
            held = session.scalars(
                select(EmpireCar).where(EmpireCar.username == account, EmpireCar.sign == "T3")
            ).one()
        self.assertEqual(still.status, "at_depot")
        self.assertEqual(still.route_json, kept)
        self.assertEqual(still.job, "waiting")
        route = empire._loads(moved.route_json, [])
        end = (route[-1][0], route[-1][1])
        self.assertEqual(moved.status, "to_better_spot")
        self.assertLessEqual(empire.haversine_km(centre, end), radius)
        self.assertNotEqual((round(end[0], 5), round(end[1], 5)), (round(sat[0], 5), round(sat[1], 5)))
        self.assertGreater(empire.haversine_km(end, sat), 0.05)
        self.assertEqual(held.status, "returning")
        self.assertEqual(held.route_json, json.dumps([[55.04, -1.60], [55.05, -1.61]]))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_completed_trip_adds_the_tariff_to_that_account(self) -> None:
        self.assertEqual(empire.north_tyneside_tariff_pence(1, 0), 490)
        self.assertEqual(empire.north_tyneside_tariff_pence(3, 0), 1040)
        self.assertEqual(empire.north_tyneside_tariff_pence(1, 120), 490)
        self.assertEqual(empire.north_tyneside_tariff_pence(3, 360), 1040)
        self.assertEqual(empire.north_tyneside_tariff_pence(0, 0), 0)
        three = empire.destination(empire.DEPOT, 90, 3.0)
        one = empire.destination(empire.DEPOT, 90, 1.0)
        with self.Session() as session:
            game = empire.ensure_game("tariffa", NOW, session)
            empire.ensure_game("tariffb", NOW, session)
            car = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "tariffa", EmpireCar.sign == "T1")
            ).one()
            car.status = "with_passenger"
            car.passenger = "Mina"
            car.route_json = json.dumps(
                [[empire.DEPOT[0], empire.DEPOT[1]], [three[0], three[1]]]
            )
            car.duration_s = 360
            car.leg_started = NOW
            car.lat, car.lng = empire.DEPOT
            empire.advance_car(car, game, NOW + 361)
            session.commit()
            self.assertEqual(car.status, "at_depot")
            self.assertAlmostEqual(car.lat, three[0], places=4)
            body = empire.snapshot("tariffa", NOW + 361, session)
            self.assertEqual(body["fare_in"], 1040)
            self.assertEqual(body["driver_share"], 520)
            second = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "tariffa", EmpireCar.sign == "T2")
            ).one()
            second.status = "with_passenger"
            second.passenger = "Owen"
            second.route_json = json.dumps(
                [[empire.DEPOT[0], empire.DEPOT[1]], [one[0], one[1]]]
            )
            second.duration_s = 120
            second.leg_started = NOW
            empire.advance_car(second, game, NOW + 121)
            session.commit()
            rival = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("tariffa", "north"),
                    EmpireCar.sign == "N1",
                )
            ).one()
            rival.status = "with_passenger"
            rival.passenger = "Priya"
            rival.firm = "north"
            rival.route_json = json.dumps(
                [[empire.DEPOT[0], empire.DEPOT[1]], [three[0], three[1]]]
            )
            rival.duration_s = 360
            rival.leg_started = NOW
            empire.advance_car(rival, game, NOW + 361)
            session.commit()
            self.assertEqual(empire.buy_car("tariffa", 800000, NOW, session), "T11")
            body = empire.snapshot("tariffa", NOW + 361, session)
            other = empire.snapshot("tariffb", NOW + 361, session)
        self.assertEqual(body["fare_in"], 1530)
        self.assertIs(type(body["fare_in"]), int)
        self.assertEqual(body["driver_share"], 765)
        self.assertEqual(body["driver_share"], body["fare_in"] // 2)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertNotEqual(body["fare_in"], 1530 + 800000)
        self.assertEqual(body["cash_pence"], -800000)
        self.assertEqual(body["new_car"], 800000)
        self.assertNotEqual(body["fare_in"], 1530 - 800000)
        self.assertEqual(other["fare_in"], 0)
        self.assertEqual(other["driver_share"], 0)
        self.assertEqual(other["cash_pence"], 0)
        self.assertEqual(len(other["cars"]), 10)
        self.assertEqual(len(body["cars"]), 11)
        self.assertNotIn("T12", [item["sign"] for item in body["cars"]])
        self.assertNotIn(2860, (body["fare_in"], body["driver_share"], body["cash_pence"]))
        self.assertNotIn(1130, (body["fare_in"], body["cash_pence"]))
        self.assertNotIn(1240, (body["fare_in"], body["cash_pence"]))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_the_feed_sends_the_loiter_patch_the_player_set(self) -> None:
        from fastapi.testclient import TestClient

        from app.main import app

        centre, radius = _empty_patch()
        with TestClient(app) as client:
            self.assertEqual(client.post("/v1/empire/loiter", json={"lat": centre[0], "lng": centre[1], "radius_km": radius}).status_code, 401)
            self.assertEqual(
                client.post("/register", json={"username": "loitpost", "password": "password1"}).status_code,
                201,
            )
            token = client.post("/login", json={"username": "loitpost", "password": "password1"}).json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}
            rejected = client.post(
                "/v1/empire/loiter",
                headers=headers,
                json={"lat": empire.BERWICK[0], "lng": empire.BERWICK[1], "radius_km": radius},
            )
            self.assertEqual(rejected.status_code, 400)
            body = client.post(
                "/v1/empire/loiter",
                headers=headers,
                json={"lat": centre[0], "lng": centre[1], "radius_km": radius, "firm": ""},
            ).json()
            again = client.get("/v1/empire", headers=headers).json()
        self.assertEqual(body["username"], "loitpost")
        self.assertEqual(body["loiter"]["firm"], "")
        self.assertAlmostEqual(body["loiter"]["lat"], centre[0], places=5)
        self.assertEqual(body["loiter"]["radius_km"], radius)
        self.assertEqual(again["loiter"]["lng"], body["loiter"]["lng"])
        self.assertEqual(body["cash_pence"], 0)
        self.assertEqual(body["fare_in"], 0)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(body["new_car"], 800000)
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_finished_firm_trip_appends_that_firms_booking(self) -> None:
        shire = empire.place_point("Shiremoor")
        cobalt = empire.place_point("Cobalt")
        tyne = empire.place_point("Tynemouth")
        bay = empire.place_point("Whitley Bay")
        named = [[shire[0], shire[1]], [cobalt[0], cobalt[1]]]
        south_route = [[tyne[0], tyne[1]], [bay[0], bay[1]]]
        one = empire.destination(empire.DEPOT, 90, 1.0)
        hired = empire.north_tyneside_tariff_pence(empire.route_miles(named), 360)
        south_pence = empire.north_tyneside_tariff_pence(empire.route_miles(south_route), 360)
        self.assertEqual(empire.north_tyneside_tariff_pence(1, 120), 490)
        self.assertNotEqual(hired, 0)
        self.assertNotEqual(hired, 800000)
        self.assertNotEqual(south_pence, 800000)
        self.assertNotIn(hired, (2860, 1130, 1240))
        self.assertNotIn(south_pence, (2860, 1130, 1240))
        kept_completed = [NOW + 1000 * (index + 1) + 360 for index in range(12)]

        def carry(car: EmpireCar, route: list, duration: float, started: float) -> None:
            car.status = "with_passenger"
            car.passenger = "Mina"
            car.route_json = json.dumps(route)
            car.duration_s = duration
            car.leg_started = started
            car.lat, car.lng = float(route[0][0]), float(route[0][1])
            car.fare_lat, car.fare_lng = float(route[0][0]), float(route[0][1])
            car.drop_lat, car.drop_lng = float(route[-1][0]), float(route[-1][1])

        with self.Session() as session:
            game = empire.ensure_game("bookfirm", NOW, session)
            empire.ensure_game("bookelse", NOW, session)
            player = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "bookfirm", EmpireCar.sign == "T1")
            ).one()
            carry(player, named, 360, NOW)
            empire.advance_car(player, game, NOW + 360)
            still = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "bookfirm", EmpireCar.sign == "T2")
            ).one()
            carry(still, named, 360, NOW)
            north = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("bookfirm", "north"),
                    EmpireCar.sign == "N1",
                )
            ).one()
            self.assertEqual(north.firm, "north")
            carry(north, [[empire.DEPOT[0], empire.DEPOT[1]], [one[0], one[1]]], 120, NOW)
            empire.advance_car(north, game, NOW + 120)
            for started in [NOW + 1000 * (index + 1) for index in range(12)]:
                carry(north, named, 360, started)
                empire.advance_car(north, game, started + 360)
            south = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("bookfirm", "south"),
                    EmpireCar.sign == "S1",
                )
            ).one()
            self.assertEqual(south.firm, "south")
            carry(south, south_route, 360, NOW + 50000)
            empire.advance_car(south, game, NOW + 50360)
            carrying = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("bookfirm", "north"),
                    EmpireCar.sign == "N2",
                )
            ).one()
            carry(carrying, named, 360, NOW)
            collecting = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("bookfirm", "south"),
                    EmpireCar.sign == "S2",
                )
            ).one()
            collecting.status = "to_passenger"
            collecting.passenger = "Owen"
            collecting.fare_name = "Owen"
            collecting.fare_lat, collecting.fare_lng = shire
            collecting.drop_lat, collecting.drop_lng = cobalt
            collecting.route_json = json.dumps(named)
            collecting.duration_s = 360
            collecting.leg_started = NOW
            session.flush()
            body = empire.snapshot("bookfirm", NOW + 1, session)
            other = empire.snapshot("bookelse", NOW + 1, session)
        firms = {firm["firm"]: firm for firm in body["firms"]}
        north_rows = firms["north"]["bookings"]
        south_rows = firms["south"]["bookings"]
        self.assertEqual(len(north_rows), 12)
        self.assertEqual([row["completed"] for row in north_rows], kept_completed)
        self.assertTrue(all(row["sign"] == "N1" for row in north_rows))
        self.assertTrue(all(row["pickup"] == "Shiremoor" and row["drop"] == "Cobalt" for row in north_rows))
        self.assertTrue(all(row["pence"] == hired for row in north_rows))
        self.assertEqual(set(north_rows[0]), {"sign", "pickup", "drop", "pence", "completed"})
        self.assertEqual(firms["north"]["fare_in"], hired * 12)
        self.assertNotIn(490, [row["pence"] for row in north_rows])
        self.assertNotIn(NOW + 120, [row["completed"] for row in north_rows])
        self.assertEqual(len(south_rows), 1)
        self.assertEqual(south_rows[0]["sign"], "S1")
        self.assertEqual(south_rows[0]["pickup"], "Tynemouth")
        self.assertEqual(south_rows[0]["drop"], "Whitley Bay")
        self.assertEqual(south_rows[0]["pence"], south_pence)
        self.assertEqual(firms["south"]["fare_in"], south_pence)
        self.assertNotEqual(firms["north"]["fare_in"], firms["south"]["fare_in"])
        self.assertEqual(body["fare_in"], hired)
        self.assertNotEqual(body["fare_in"], hired + firms["north"]["fare_in"])
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertEqual(body["cash_pence"], 0)
        self.assertEqual(len(body["cars"]), 10)
        parked = self._car(body, "T1")
        self.assertEqual(parked["status"], "at_depot")
        self.assertEqual(parked["trip_pence"], 0)
        self.assertEqual(parked["pickup"], "")
        self.assertEqual(parked["drop"], "")
        own = self._car(body, "T2")
        self.assertEqual(own["status"], "with_passenger")
        self.assertEqual(own["pickup"], "Shiremoor")
        self.assertEqual(own["drop"], "Cobalt")
        self.assertEqual(own["trip_pence"], hired)
        waiting = self._car(body, "T3")
        self.assertEqual(waiting["trip_pence"], 0)
        self.assertEqual(waiting["pickup"], "")
        north_cars = {car["sign"]: car for car in firms["north"]["cars"]}
        self.assertEqual(north_cars["N1"]["trip_pence"], 0)
        self.assertEqual(north_cars["N1"]["pickup"], "")
        self.assertEqual(north_cars["N2"]["pickup"], "Shiremoor")
        self.assertEqual(north_cars["N2"]["drop"], "Cobalt")
        self.assertEqual(north_cars["N2"]["trip_pence"], hired)
        self.assertNotEqual(north_cars["N2"]["trip_pence"], 800000)
        self.assertEqual(north_cars["N3"]["status"], "at_depot")
        self.assertEqual(north_cars["N3"]["trip_pence"], 0)
        self.assertEqual(north_cars["N3"]["pickup"], "")
        south_cars = {car["sign"]: car for car in firms["south"]["cars"]}
        miles = empire.haversine_km(shire, cobalt) / empire.KM_PER_MILE
        seconds = max(miles, 0.001) / empire.MPH * 3600.0
        self.assertEqual(south_cars["S2"]["status"], "to_passenger")
        self.assertEqual(south_cars["S2"]["pickup"], "Shiremoor")
        self.assertEqual(south_cars["S2"]["drop"], "Cobalt")
        self.assertEqual(south_cars["S2"]["trip_pence"], empire.north_tyneside_tariff_pence(miles, seconds))
        self.assertNotEqual(south_cars["S2"]["trip_pence"], 0)
        self.assertNotEqual(south_cars["S2"]["trip_pence"], 800000)
        for firm in other["firms"]:
            self.assertEqual(firm["bookings"], [])
            self.assertEqual(firm["fare_in"], 0)
        self.assertEqual(other["fare_in"], 0)
        self.assertNotIn(2860, (body["fare_in"], firms["north"]["fare_in"], firms["south"]["fare_in"]))
        self.assertNotIn(1130, (body["fare_in"], firms["north"]["fare_in"], firms["south"]["fare_in"]))
        self.assertNotIn(1240, (body["fare_in"], firms["north"]["fare_in"], firms["south"]["fare_in"], hired, south_pence))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_job_cancelled_before_the_car_moves_adds_no_fare_and_no_fuel(self) -> None:
        shire = empire.place_point("Shiremoor")
        cobalt = empire.place_point("Cobalt")
        route = [[shire[0], shire[1]], [cobalt[0], cobalt[1]]]
        self.assertGreater(empire.haversine_km(shire, cobalt) * 0.5, 0.05)
        self.assertLess(empire.haversine_km(shire, cobalt) / 360, 0.05)

        def collecting(car: EmpireCar, started: float) -> None:
            car.status = "to_passenger"
            car.job = "collecting"
            car.passenger = "Mina"
            car.fare_name = "Mina"
            car.lat, car.lng = shire
            car.fare_lat, car.fare_lng = shire
            car.drop_lat, car.drop_lng = cobalt
            car.route_json = json.dumps(route)
            car.duration_s = 360
            car.leg_started = started
            car.hold_until = started

        with self.Session() as session:
            game = empire.ensure_game("stilljob", NOW, session)
            empire.ensure_game("stilloth", NOW, session)
            game.fare_in = 1040
            game.miles_driven = 3
            game.cash_pence = 40
            session.commit()
            first = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T1")
            ).one()
            second = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T2")
            ).one()
            collecting(first, NOW)
            collecting(second, NOW)
            session.commit()
            self.assertTrue(empire.cancel_job("stilljob", "T1", NOW, session))
            self.assertFalse(empire.cancel_job("stilljob", "", NOW, session))
            self.assertFalse(empire.cancel_job("stilljob", "T99", NOW, session))
            north = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("stilljob", "north"),
                    EmpireCar.sign == "N1",
                )
            ).one()
            collecting(north, NOW)
            session.commit()
            self.assertFalse(empire.cancel_job("stilljob", "N1", NOW, session))
            self.assertEqual(north.status, "to_passenger")
            self.assertEqual(north.passenger, "Mina")
            body = empire.snapshot("stilljob", NOW, session)
            other = empire.snapshot("stilloth", NOW, session)
            fresh = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T8")
            ).one()
            collecting(fresh, NOW)
            session.commit()
            self.assertTrue(empire.cancel_job("stilljob", "T8", NOW + 1, session))
            third = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T3")
            ).one()
            self.assertEqual(third.status, "at_depot")
            person = empire.EmpirePerson(
                username="stilljob",
                name="Owen",
                lat=shire[0],
                lng=shire[1],
                drop_lat=cobalt[0],
                drop_lng=cobalt[1],
                sign="T3",
                arrived=NOW,
            )
            session.add(person)
            session.flush()
            self.assertTrue(empire.cancel_job("stilljob", "T3", NOW, session))
            self.assertEqual(person.sign, "")
            self.assertEqual(third.status, "at_depot")
            carried = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T4")
            ).one()
            carried.status = "with_passenger"
            carried.passenger = "Priya"
            carried.fare_name = "Priya"
            carried.lat, carried.lng = shire
            carried.route_json = json.dumps(route)
            carried.duration_s = 360
            carried.leg_started = NOW
            empty = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T5")
            ).one()
            empty.status = "to_passenger"
            empty.passenger = ""
            empty.fare_name = ""
            empty.lat, empty.lng = shire
            empty.route_json = json.dumps(route)
            empty.duration_s = 360
            empty.leg_started = NOW
            hunting = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T6")
            ).one()
            hunting.status = "to_better_spot"
            hunting.passenger = ""
            hunting.lat, hunting.lng = shire
            hunting.route_json = json.dumps(route)
            hunting.duration_s = 360
            hunting.leg_started = NOW
            home = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "stilljob", EmpireCar.sign == "T7")
            ).one()
            home.status = "returning"
            home.passenger = ""
            home.lat, home.lng = shire
            home.route_json = json.dumps(route)
            home.duration_s = 360
            home.leg_started = NOW
            session.commit()
            self.assertFalse(empire.cancel_job("stilljob", "T4", NOW, session))
            self.assertFalse(empire.cancel_job("stilljob", "T5", NOW, session))
            self.assertFalse(empire.cancel_job("stilljob", "T6", NOW, session))
            self.assertFalse(empire.cancel_job("stilljob", "T7", NOW, session))
            self.assertEqual(carried.status, "with_passenger")
            self.assertEqual(carried.passenger, "Priya")
            self.assertEqual(empty.status, "to_passenger")
            self.assertEqual(hunting.status, "to_better_spot")
            self.assertEqual(home.status, "returning")
            self.assertEqual(game.fare_in, 1040)
            self.assertEqual(game.miles_driven, 3)
            self.assertEqual(game.cash_pence, 40)
            later = empire.snapshot("stilljob", NOW + 180, session)
            moving = sum(
                empire.driven_miles(car, NOW + 180)
                for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "stilljob")).all()
                if not (car.firm or "")
            )
            released = empire.snapshot("stilljob", NOW, session)
        parked = self._car(body, "T1")
        waited = self._car(body, "T2")
        self.assertEqual(parked["status"], "at_depot")
        self.assertEqual(parked["job"], "waiting")
        self.assertEqual(parked["passenger"], "")
        self.assertEqual(parked["trip_pence"], 0)
        self.assertEqual(parked["pickup"], "")
        self.assertEqual(parked["drop"], "")
        self.assertEqual(waited["status"], "to_passenger")
        self.assertEqual(body["fare_in"], 1040)
        self.assertEqual(body["driver_share"], 520)
        self.assertEqual(body["fuel"], empire.fuel_pence(3))
        self.assertEqual(body["cash_pence"], 40)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(other["fare_in"], 0)
        self.assertEqual(other["fuel"], 0)
        self.assertEqual(other["cash_pence"], 0)
        self.assertEqual(len(other["cars"]), 10)
        self.assertEqual(self._car(later, "T2")["status"], "to_passenger")
        self.assertEqual(self._car(later, "T8")["status"], "at_depot")
        self.assertEqual(self._car(later, "T8")["passenger"], "")
        self.assertEqual(self._car(later, "T8")["trip_pence"], 0)
        self.assertEqual(self._car(later, "T2")["passenger"], "Mina")
        self.assertEqual(later["fare_in"], 1040)
        self.assertEqual(later["cash_pence"], 40)
        self.assertEqual(later["fuel"], empire.fuel_pence(3 + moving))
        self.assertGreater(moving, 0)
        self.assertEqual(later["driver_share"], 520)
        self.assertEqual(len(later["cars"]), 10)
        owen = next(row for row in released["people"] if row["name"] == "Owen")
        self.assertEqual(owen["sign"], "")
        for firm in later["firms"]:
            self.assertEqual(firm["bookings"], [])
        self.assertNotEqual(later["fare_in"], 800000)
        self.assertNotEqual(later["fuel"], 800000)
        self.assertNotIn(2860, (later["fare_in"], later["fuel"], later["cash_pence"], parked["trip_pence"]))
        self.assertNotIn(1130, (later["fare_in"], later["fuel"], later["cash_pence"]))
        self.assertNotIn(1240, (later["fare_in"], later["fuel"], later["cash_pence"]))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())

    def test_a_job_cancelled_after_the_car_has_left_adds_fuel_and_no_fare(self) -> None:
        shire = empire.place_point("Shiremoor")
        cobalt = empire.place_point("Cobalt")
        route = [[shire[0], shire[1]], [cobalt[0], cobalt[1]]]
        full = empire.route_miles(route)
        tariff = empire.north_tyneside_tariff_pence(full, 360)
        self.assertGreater(full, 0)
        self.assertNotEqual(tariff, 0)
        self.assertNotEqual(tariff, 800000)
        self.assertNotIn(tariff, (2860, 1130, 1240))

        def passenger(car: EmpireCar, status: str, started: float) -> None:
            car.status = status
            car.passenger = "Mina"
            car.fare_name = "Mina"
            car.lat, car.lng = shire
            car.fare_lat, car.fare_lng = shire
            car.drop_lat, car.drop_lng = cobalt
            car.route_json = json.dumps(route)
            car.duration_s = 360
            car.leg_started = started
            car.hold_until = started

        with self.Session() as session:
            game = empire.ensure_game("leftjob", NOW, session)
            empire.ensure_game("leftoth", NOW, session)
            game.fare_in = 1040
            game.miles_driven = 3
            game.cash_pence = 40
            session.commit()
            first = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "leftjob", EmpireCar.sign == "T1")
            ).one()
            passenger(first, "to_passenger", NOW)
            person = empire.EmpirePerson(
                username="leftjob",
                name="Mina",
                lat=shire[0],
                lng=shire[1],
                drop_lat=cobalt[0],
                drop_lng=cobalt[1],
                sign="T1",
                arrived=NOW,
            )
            session.add(person)
            session.commit()
            partial = empire.driven_miles(first, NOW + 180)
            self.assertAlmostEqual(partial, full * 0.5, places=5)
            self.assertTrue(empire.cancel_job("leftjob", "T1", NOW + 180, session))
            self.assertEqual(person.sign, "")
            self.assertEqual(first.status, "at_depot")
            self.assertEqual(first.passenger, "")
            self.assertEqual(first.job, "waiting")
            self.assertEqual(empire.driven_miles(first, NOW + 180), 0)
            self.assertAlmostEqual(game.miles_driven, 3 + partial, places=5)
            self.assertEqual(game.fare_in, 1040)
            self.assertFalse(empire.cancel_job("leftjob", "T1", NOW + 181, session))
            self.assertAlmostEqual(game.miles_driven, 3 + partial, places=5)
            second = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "leftjob", EmpireCar.sign == "T2")
            ).one()
            passenger(second, "with_passenger", NOW)
            session.commit()
            carried = empire.driven_miles(second, NOW + 180)
            self.assertAlmostEqual(carried, full * 0.5, places=5)
            self.assertTrue(empire.cancel_job("leftjob", "T2", NOW + 180, session))
            self.assertEqual(second.status, "at_depot")
            self.assertEqual(second.passenger, "")
            self.assertAlmostEqual(game.miles_driven, 3 + partial + carried, places=5)
            self.assertLess(game.miles_driven, 3 + full + full)
            self.assertEqual(game.fare_in, 1040)
            self.assertNotEqual(game.fare_in, 1040 + tariff)
            ninth = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "leftjob", EmpireCar.sign == "T9")
            ).one()
            passenger(ninth, "to_passenger", NOW)
            ninth.lat, ninth.lng = cobalt
            session.commit()
            self.assertTrue(empire.cancel_job("leftjob", "T9", NOW, session))
            self.assertEqual(ninth.status, "at_depot")
            self.assertAlmostEqual(game.miles_driven, 3 + partial + carried, places=5)
            north = session.scalars(
                select(EmpireCar).where(
                    EmpireCar.username == empire.firm_key("leftjob", "north"),
                    EmpireCar.sign == "N1",
                )
            ).one()
            passenger(north, "to_passenger", NOW)
            north.lat, north.lng = cobalt
            session.commit()
            self.assertFalse(empire.cancel_job("leftjob", "N1", NOW + 180, session))
            self.assertEqual(north.status, "to_passenger")
            self.assertEqual(north.passenger, "Mina")
            self.assertAlmostEqual(game.miles_driven, 3 + partial + carried, places=5)
            empty = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "leftjob", EmpireCar.sign == "T5")
            ).one()
            empty.status = "to_passenger"
            empty.passenger = ""
            empty.fare_name = ""
            empty.lat, empty.lng = shire
            empty.route_json = json.dumps(route)
            empty.duration_s = 360
            empty.leg_started = NOW
            hunting = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "leftjob", EmpireCar.sign == "T6")
            ).one()
            hunting.status = "to_better_spot"
            hunting.passenger = ""
            hunting.fare_name = ""
            hunting.lat, hunting.lng = shire
            hunting.route_json = json.dumps(route)
            hunting.duration_s = 360
            hunting.leg_started = NOW
            home = session.scalars(
                select(EmpireCar).where(EmpireCar.username == "leftjob", EmpireCar.sign == "T7")
            ).one()
            home.status = "returning"
            home.passenger = ""
            home.lat, home.lng = shire
            home.route_json = json.dumps(route)
            home.duration_s = 360
            home.leg_started = NOW
            session.commit()
            kept = game.miles_driven
            self.assertFalse(empire.cancel_job("leftjob", "T5", NOW + 180, session))
            self.assertFalse(empire.cancel_job("leftjob", "T6", NOW + 180, session))
            self.assertFalse(empire.cancel_job("leftjob", "T7", NOW + 180, session))
            self.assertEqual(empty.status, "to_passenger")
            self.assertEqual(hunting.status, "to_better_spot")
            self.assertEqual(home.status, "returning")
            self.assertAlmostEqual(game.miles_driven, kept, places=5)
            self.assertEqual(game.fare_in, 1040)
            self.assertEqual(game.cash_pence, 40)
            body = empire.snapshot("leftjob", NOW + 180, session)
            moving = sum(
                empire.driven_miles(car, NOW + 180)
                for car in session.scalars(select(EmpireCar).where(EmpireCar.username == "leftjob")).all()
                if not (car.firm or "")
            )
            other = empire.snapshot("leftoth", NOW + 180, session)
        self.assertEqual(self._car(body, "T1")["status"], "at_depot")
        self.assertEqual(self._car(body, "T1")["trip_pence"], 0)
        self.assertEqual(self._car(body, "T1")["pickup"], "")
        self.assertEqual(self._car(body, "T2")["trip_pence"], 0)
        self.assertEqual(self._car(body, "T2")["passenger"], "")
        self.assertEqual(body["fare_in"], 1040)
        self.assertEqual(body["driver_share"], 520)
        self.assertEqual(body["cash_pence"], 40)
        self.assertEqual(body["fuel"], empire.fuel_pence(3 + partial + carried + moving))
        self.assertNotEqual(body["fuel"], empire.fuel_pence(3))
        self.assertNotEqual(body["fuel"], empire.fuel_pence(3 + full + full))
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(other["fare_in"], 0)
        self.assertEqual(other["fuel"], 0)
        self.assertEqual(other["cash_pence"], 0)
        self.assertEqual(other["cars"] and len(other["cars"]), 10)
        for firm in body["firms"]:
            self.assertEqual(firm["bookings"], [])
            self.assertNotEqual(firm["fare_in"], tariff)
        self.assertNotEqual(body["fare_in"], 800000)
        self.assertNotEqual(body["fuel"], 800000)
        self.assertNotIn(2860, (body["fare_in"], body["fuel"], body["cash_pence"], tariff))
        self.assertNotIn(1130, (body["fare_in"], body["fuel"], body["cash_pence"]))
        self.assertNotIn(1240, (body["fare_in"], body["fuel"], body["cash_pence"], tariff))
        self.assertNotEqual(Path(TEMP_DB).resolve(), (ROOT / "hackathon.db").resolve())


if __name__ == "__main__":
    unittest.main()
