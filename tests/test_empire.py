"""Account fleet. Game rows are never devices, and tests never open the live database."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_fd, TEMP_DB = tempfile.mkstemp(prefix="empire-test-", suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + TEMP_DB.replace("\\", "/")
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import sessionmaker

import app.empire as empire
from app.database import Base, SessionLocal
from app.models import Device, DeviceLink, EmpireCar, EmpireGame, LocationPing  # noqa: F401

empire.set_route_fn(lambda start, end: None)
NOW = 1_700_000_000


class EmpireTableTests(unittest.TestCase):
    def test_empire_tables_have_no_device_id(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        insp = inspect(engine)
        names = set(insp.get_table_names())
        self.assertIn("empire_games", names)
        self.assertIn("empire_cars", names)
        for table in ("empire_games", "empire_cars"):
            columns = {col["name"] for col in insp.get_columns(table)}
            self.assertNotIn("device_id", columns)
        device_columns = {col["name"] for col in insp.get_columns("devices")}
        self.assertIn("device_id", device_columns)
        self.assertIn("device_id", {col["name"] for col in insp.get_columns("location_pings")})
        self.assertIn("device_id", {col["name"] for col in insp.get_columns("device_links")})


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
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            body = empire.snapshot("ada", NOW + 64, session)
        self.assertTrue(body["osrm_unavailable"])
        self.assertEqual(len(body["cars"]), 10)
        for index in range(1, 11):
            car = self._car(body, f"T{index}")
            self.assertEqual(car["status"], "to_passenger")
            self.assertFalse(car["osrm"])
            self.assertEqual(len(car["route"]), 2)
            start = (car["route"][0][0], car["route"][0][1])
            end = (car["route"][1][0], car["route"][1][1])
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
            to_s = self._car(body, "T1")["duration_s"]
            empire.tick_all(NOW + to_s + 1, session)
            body = empire.snapshot("ada", NOW + to_s + 1, session)
            self.assertEqual(self._car(body, "T1")["status"], "with_passenger")
            self.assertFalse([fare for fare in body["fares"] if fare["sign"] == "T1"])
            with_s = self._car(body, "T1")["duration_s"]
            back = NOW + to_s + with_s
            empire.tick_all(back + 1, session)
            body = empire.snapshot("ada", back + 1, session)
            self.assertEqual(self._car(body, "T1")["status"], "returning")
            ret_s = self._car(body, "T1")["duration_s"]
            home = back + ret_s
            empire.tick_all(home + 1, session)
            body = empire.snapshot("ada", home + 1, session)
            self.assertEqual(self._car(body, "T1")["status"], "at_depot")
            empire.tick_all(home + 3, session)
            body = empire.snapshot("ada", home + 3, session)
            self.assertEqual(self._car(body, "T1")["status"], "at_depot")
            empire.tick_all(home + 5, session)
            body = empire.snapshot("ada", home + 5, session)
            self.assertEqual(self._car(body, "T1")["status"], "to_passenger")

    def test_each_of_the_ten_waits_four_seconds(self) -> None:
        self.assertEqual(empire.DEPOT_PAUSE_S, 4)
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for car in cars:
                car.status = "returning"
                car.route_json = "[[55.0367, -1.5075], [55.0367, -1.5075]]"
                car.duration_s = 10
                car.leg_started = NOW
                car.hold_until = NOW
            session.commit()
            empire.tick_all(NOW + 10, session)
            arrived = empire.snapshot("ada", NOW + 10, session)
            empire.tick_all(NOW + 13, session)
            waiting = empire.snapshot("ada", NOW + 13, session)
            empire.tick_all(NOW + 14, session)
            left = empire.snapshot("ada", NOW + 14, session)
        for index in range(1, 11):
            sign = f"T{index}"
            self.assertEqual(self._car(arrived, sign)["status"], "at_depot")
            self.assertEqual(self._car(waiting, sign)["status"], "at_depot")
            self.assertEqual(self._car(left, sign)["status"], "to_passenger")

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
            if empire.trip_bearing(a) != empire.trip_bearing(b):
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
        for index in range(80):
            one_name = f"bear{index}a"
            other_name = f"bear{index}b"
            if empire.trip_bearing(one_name) != empire.trip_bearing(other_name):
                left, right = one_name, other_name
                break
        self.assertIsNotNone(left)
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
        expected_left = empire.destination(empire.DEPOT, empire.trip_bearing(left), empire.PICKUP_MILES)
        expected_right = empire.destination(empire.DEPOT, empire.trip_bearing(right), empire.PICKUP_MILES)
        self.assertEqual(len(left_cars), 10)
        self.assertEqual(len(right_cars), 10)
        self.assertEqual(len(one["fares"]), 10)
        self.assertEqual(len(other["fares"]), 10)
        for car in left_cars:
            self.assertAlmostEqual(car.fare_lat, expected_left[0], places=4)
            self.assertAlmostEqual(car.fare_lng, expected_left[1], places=4)
        for car in right_cars:
            self.assertAlmostEqual(car.fare_lat, expected_right[0], places=4)
            self.assertAlmostEqual(car.fare_lng, expected_right[1], places=4)
        self.assertNotEqual((round(expected_left[0], 5), round(expected_left[1], 5)), (round(expected_right[0], 5), round(expected_right[1], 5)))
        other_points = {(round(fare["lat"], 5), round(fare["lng"], 5)) for fare in other["fares"]}
        for fare in one["fares"]:
            self.assertNotIn((round(fare["lat"], 5), round(fare["lng"], 5)), other_points)

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
        self.assertEqual(seen[:6], ["Mina", "Owen", "Priya", "Jack", "Helen", "Tom"])

    def test_ten_car_names_cycle_mina_through_tom(self) -> None:
        cycle = ("Mina", "Owen", "Priya", "Jack", "Helen", "Tom")
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            body = empire.snapshot("ada", NOW + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        names = [self._car(body, f"T{index}")["passenger"] for index in range(1, 11)]
        self.assertEqual(names, [cycle[(index - 1) % len(cycle)] for index in range(1, 11)])
        self.assertEqual(names[0], "Mina")
        self.assertEqual(names[5], "Tom")
        self.assertEqual(names[6], "Mina")

    def test_collected_ten_car_fare_leaves_the_feed(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            waiting = empire.snapshot("ada", NOW + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            for car in cars:
                car.duration_s = 10
                car.leg_started = NOW
                car.route_json = "[[55.0367, -1.5075], [55.04, -1.51]]"
            session.commit()
            empire.tick_all(NOW + 10, session)
            arrived = empire.snapshot("ada", NOW + 10, session)
        self.assertEqual(len(waiting["fares"]), 10)
        self.assertEqual({fare["sign"] for fare in waiting["fares"]}, {f"T{index}" for index in range(1, 11)})
        self.assertEqual(arrived["fares"], [])
        for index in range(1, 11):
            self.assertEqual(self._car(arrived, f"T{index}")["status"], "with_passenger")

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
            empire.ensure_game("ada", NOW, session)
            with self.assertLogs("hackathon-api", level="ERROR"):
                empire.tick_all(NOW + 64, session)
            body = empire.snapshot("ada", NOW + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        self.assertEqual(self._car(body, "T4")["status"], "at_depot")
        for index in range(1, 11):
            if index == 4:
                continue
            self.assertEqual(self._car(body, f"T{index}")["status"], "to_passenger", f"T{index}")

    def test_first_departures_are_staggered_by_about_7_seconds(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            early = empire.snapshot("ada", NOW + 1, session)
            empire.tick_all(NOW + 8, session)
            middle = empire.snapshot("ada", NOW + 8, session)
            empire.tick_all(NOW + 15, session)
            late = empire.snapshot("ada", NOW + 15, session)
        self.assertEqual(self._car(early, "T1")["status"], "to_passenger")
        self.assertEqual(self._car(early, "T2")["status"], "at_depot")
        self.assertEqual(self._car(early, "T3")["status"], "at_depot")
        self.assertEqual(self._car(middle, "T2")["status"], "to_passenger")
        self.assertEqual(self._car(middle, "T3")["status"], "at_depot")
        self.assertEqual(self._car(late, "T3")["status"], "to_passenger")

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
        pickup_miles = empire.haversine_km(empire.DEPOT, pickup) / empire.KM_PER_MILE
        drop_miles = empire.haversine_km(empire.DEPOT, drop) / empire.KM_PER_MILE
        self.assertAlmostEqual(pickup_miles, 1.2, delta=0.05)
        self.assertGreater(drop_miles, pickup_miles)
        self.assertTrue(empire.inside_circle(pickup))
        self.assertTrue(empire.inside_circle(drop))
        self.assertGreater(empire.haversine_km(pickup, empire.BERWICK), 40)
        self.assertGreater(empire.haversine_km(drop, empire.BERWICK), 40)

    def test_ten_car_trips_stay_inside_the_circle(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
        self.assertEqual(len(cars), 10)
        for car in cars:
            pickup = (car.fare_lat, car.fare_lng)
            drop = (car.drop_lat, car.drop_lng)
            self.assertTrue(empire.inside_circle(pickup), car.sign)
            self.assertTrue(empire.inside_circle(drop), car.sign)
            self.assertNotEqual(pickup, empire.BERWICK)
            self.assertNotEqual(drop, empire.BERWICK)
            self.assertGreater(empire.haversine_km(pickup, empire.BERWICK), 40)
            self.assertGreater(empire.haversine_km(drop, empire.BERWICK), 40)

    def test_ten_car_drop_off_is_further_than_the_pickup(self) -> None:
        bearing = empire.trip_bearing("ada")
        expected_pickup = empire.destination(empire.DEPOT, bearing, empire.PICKUP_MILES)
        expected_drop = empire.destination(empire.DEPOT, bearing, empire.DROP_MILES)
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
        self.assertEqual(len(cars), 10)
        self.assertEqual(empire.PICKUP_MILES, 1.2)
        self.assertGreater(empire.DROP_MILES, empire.PICKUP_MILES)
        for car in cars:
            pickup = (car.fare_lat, car.fare_lng)
            drop = (car.drop_lat, car.drop_lng)
            pickup_miles = empire.haversine_km(empire.DEPOT, pickup) / empire.KM_PER_MILE
            drop_miles = empire.haversine_km(empire.DEPOT, drop) / empire.KM_PER_MILE
            self.assertAlmostEqual(pickup_miles, 1.2, delta=0.05, msg=car.sign)
            self.assertGreater(drop_miles, pickup_miles, car.sign)
            self.assertAlmostEqual(car.fare_lat, expected_pickup[0], places=4, msg=car.sign)
            self.assertAlmostEqual(car.fare_lng, expected_pickup[1], places=4, msg=car.sign)
            self.assertAlmostEqual(car.drop_lat, expected_drop[0], places=4, msg=car.sign)
            self.assertAlmostEqual(car.drop_lng, expected_drop[1], places=4, msg=car.sign)
            self.assertTrue(empire.inside_circle(pickup), car.sign)
            self.assertTrue(empire.inside_circle(drop), car.sign)

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
            empire.tick_all(NOW + 64, session)
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
        self.assertEqual(self._car(left, "T11")["status"], "to_passenger")

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
            empire.tick_all(NOW + 64, session)
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
                "fare_in",
                "driver_share",
                "fuel",
                "standing",
                "new_car",
            },
        )
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
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            hit = empire.snapshot("ada", NOW + 64, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
            self.assertEqual(len(cars), 10)
            missed = next(car for car in cars if car.sign == "T4")
            missed.osrm = False
            session.commit()
            mixed = empire.snapshot("ada", NOW + 64, session)
        self.assertEqual(len(hit["cars"]), 10)
        self.assertFalse(hit["osrm_unavailable"])
        for index in range(1, 11):
            car = self._car(hit, f"T{index}")
            self.assertEqual(car["status"], "to_passenger")
            self.assertTrue(car["osrm"])
        self.assertTrue(mixed["osrm_unavailable"])
        self.assertFalse(self._car(mixed, "T4")["osrm"])
        for index in range(1, 11):
            if index == 4:
                continue
            self.assertTrue(self._car(mixed, f"T{index}")["osrm"])

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
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 64, session)
            body = empire.snapshot("ada", NOW + 64, session)
        self.assertEqual(len(body["cars"]), 10)
        for index in range(1, 11):
            car = self._car(body, f"T{index}")
            self.assertEqual(car["status"], "to_passenger")
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
        for car in driving["cars"]:
            self.assertIn(car["status"], allowed)
        self.assertEqual({car["status"] for car in mixed["cars"]}, set(allowed))
        for car in mixed["cars"]:
            self.assertIn(car["status"], allowed)

    def test_suite_does_not_use_the_live_database_or_the_network(self) -> None:
        live = ROOT / "hackathon.db"
        before = live.stat().st_mtime if live.exists() else None
        self.assertFalse(empire.clock_running())
        self.assertTrue(TEMP_DB.endswith(".db"))
        after = live.stat().st_mtime if live.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
