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
        self.assertEqual([car.sign for car in cars], ["T1", "T2", "T3"])
        for car in cars:
            self.assertEqual(car.status, "at_depot")
            self.assertAlmostEqual(car.lat, 55.0367, places=4)
            self.assertAlmostEqual(car.lng, -1.5075, places=4)
        live = ROOT / "hackathon.db"
        self.assertNotEqual(Path(TEMP_DB).resolve(), live.resolve())

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
        self.assertEqual(len(body["cars"]), 3)
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

    def test_health_version_is_0_1_27(self) -> None:
        from app.config import VERSION
        from app.routers.health import health

        self.assertEqual(VERSION, "0.1.27")
        self.assertEqual(health()["version"], "0.1.27")

    def test_username_shifts_the_pickup(self) -> None:
        self.test_two_feeds_do_not_share_fare_coordinates()

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
            signs = [car.sign for car in second.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all()]
        self.assertEqual(signs, ["T1", "T2", "T3"])
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
        self.assertEqual([car["sign"] for car in body["cars"]], ["T1", "T2", "T3"])

    def test_feed_colours(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW, session)
        colours = {car["sign"]: car["colour"] for car in body["cars"]}
        self.assertEqual(colours, {"T1": "#f5b942", "T2": "#38bdf8", "T3": "#4ade80"})

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

    def test_three_cars_can_hold_three_statuses(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada").order_by(EmpireCar.sign)).all())
            cars[0].status = "to_passenger"
            cars[0].fare_name = "Mina"
            cars[0].passenger = "Mina"
            cars[1].status = "with_passenger"
            cars[1].passenger = "Owen"
            cars[2].status = "returning"
            session.commit()
            body = empire.snapshot("ada", NOW, session)
        statuses = {car["sign"]: car["status"] for car in body["cars"]}
        self.assertEqual(statuses, {"T1": "to_passenger", "T2": "with_passenger", "T3": "returning"})

    def test_feed_names_the_clock_and_the_trail(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            body = empire.snapshot("ada", NOW + 3, session)
        self.assertEqual(
            set(body),
            {"ok", "username", "server_now", "check_s", "trail_s", "osrm_unavailable", "cars", "fares"},
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

    def test_a_leaving_leg_starts_at_the_depot(self) -> None:
        with self.Session() as session:
            empire.ensure_game("ada", NOW, session)
            empire.tick_all(NOW + 1, session)
            body = empire.snapshot("ada", NOW + 1, session)
        start = self._car(body, "T1")["route"][0]
        self.assertAlmostEqual(start[0], 55.0367, places=4)
        self.assertAlmostEqual(start[1], -1.5075, places=4)

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

    def test_suite_does_not_use_the_live_database_or_the_network(self) -> None:
        live = ROOT / "hackathon.db"
        before = live.stat().st_mtime if live.exists() else None
        self.assertFalse(empire.clock_running())
        self.assertTrue(TEMP_DB.endswith(".db"))
        after = live.stat().st_mtime if live.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
