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

from sqlalchemy import create_engine, inspect, select

from app.database import Base, SessionLocal
from app.models import Device, DeviceLink, EmpireCar, LocationPing  # noqa: F401


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
            created = client.post("/register", json={"username": "Ada", "password": "password1"})
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["username"], "ada")
        with SessionLocal() as session:
            cars = list(session.scalars(select(EmpireCar).where(EmpireCar.username == "ada")).all())
        self.assertEqual([car.sign for car in cars], ["T1", "T2", "T3"])
        for car in cars:
            self.assertEqual(car.status, "at_depot")
            self.assertAlmostEqual(car.lat, 55.0367, places=4)
            self.assertAlmostEqual(car.lng, -1.5075, places=4)
        live = ROOT / "hackathon.db"
        self.assertNotEqual(Path(TEMP_DB).resolve(), live.resolve())


if __name__ == "__main__":
    unittest.main()
