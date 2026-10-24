"""SQLAlchemy engine and session for the local SQLite file."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL, log

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_wal(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


class Base(DeclarativeBase):
    pass


class PersistError(Exception):
    """SQLite would not accept the write."""


class UserExistsError(Exception):
    """Username already has a users row."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise_username(username: str) -> str:
    return username.strip().lower()


def ensure_empire_fare_column(bind=None) -> None:
    """Add fare_in to an existing empire_games table. Do not wipe rows."""
    from sqlalchemy import inspect, text

    target = engine if bind is None else bind
    insp = inspect(target)
    if "empire_games" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("empire_games")}
    if "fare_in" in cols:
        return
    with target.begin() as conn:
        conn.execute(text("ALTER TABLE empire_games ADD COLUMN fare_in INTEGER NOT NULL DEFAULT 0"))


def ensure_empire_loiter_columns(bind=None) -> None:
    """Add the loiter patch to an existing empire_games table. Do not wipe rows."""
    from sqlalchemy import inspect, text

    target = engine if bind is None else bind
    insp = inspect(target)
    if "empire_games" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("empire_games")}
    wanted = {
        "loiter_firm": "VARCHAR(16) NOT NULL DEFAULT ''",
        "loiter_lat": "FLOAT NOT NULL DEFAULT 0",
        "loiter_lng": "FLOAT NOT NULL DEFAULT 0",
        "loiter_km": "FLOAT NOT NULL DEFAULT 0",
    }
    missing = [name for name in wanted if name not in cols]
    if not missing:
        return
    with target.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE empire_games ADD COLUMN {name} {wanted[name]}"))


def ensure_empire_cash_column(bind=None) -> None:
    """Add cash_pence to an existing empire_games table. Do not wipe rows."""
    from sqlalchemy import inspect, text

    target = engine if bind is None else bind
    insp = inspect(target)
    if "empire_games" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("empire_games")}
    if "cash_pence" in cols:
        return
    with target.begin() as conn:
        conn.execute(text("ALTER TABLE empire_games ADD COLUMN cash_pence INTEGER NOT NULL DEFAULT 0"))


def ensure_empire_person_drop_columns(bind=None) -> None:
    """Add the drawn drop to an existing empire_people table. Do not wipe rows."""
    from sqlalchemy import inspect, text

    target = engine if bind is None else bind
    insp = inspect(target)
    if "empire_people" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("empire_people")}
    missing = [name for name in ("drop_lat", "drop_lng") if name not in cols]
    if not missing:
        return
    with target.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE empire_people ADD COLUMN {name} FLOAT NOT NULL DEFAULT 0"))


def ensure_empire_person_arrived_column(bind=None) -> None:
    """Add the arrival time to an existing empire_people table. Do not wipe rows."""
    from sqlalchemy import inspect, text

    target = engine if bind is None else bind
    insp = inspect(target)
    if "empire_people" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("empire_people")}
    if "arrived" in cols:
        return
    with target.begin() as conn:
        conn.execute(text("ALTER TABLE empire_people ADD COLUMN arrived FLOAT NOT NULL DEFAULT 0"))


def ensure_empire_job_column(bind=None) -> None:
    """Add job to an existing empire_cars table. Do not wipe rows."""
    from sqlalchemy import inspect, text

    target = engine if bind is None else bind
    insp = inspect(target)
    if "empire_cars" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("empire_cars")}
    if "job" in cols:
        return
    with target.begin() as conn:
        conn.execute(text("ALTER TABLE empire_cars ADD COLUMN job VARCHAR(64) NOT NULL DEFAULT ''"))


def ensure_empire_firm_columns(bind=None) -> None:
    """Add firm and its depot to an existing empire_cars table. Do not wipe rows."""
    from sqlalchemy import inspect, text

    target = engine if bind is None else bind
    insp = inspect(target)
    if "empire_cars" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("empire_cars")}
    wanted = {
        "firm": "VARCHAR(16) NOT NULL DEFAULT ''",
        "depot_lat": "FLOAT NOT NULL DEFAULT 0",
        "depot_lng": "FLOAT NOT NULL DEFAULT 0",
    }
    missing = [name for name in wanted if name not in cols]
    if not missing:
        return
    with target.begin() as conn:
        for name in missing:
            conn.execute(text(f"ALTER TABLE empire_cars ADD COLUMN {name} {wanted[name]}"))


def init_db() -> None:
    from . import models  # noqa: F401 — register tables
    from sqlalchemy import inspect, text

    if DATABASE_URL.startswith("sqlite"):
        Path(engine.url.database or "hackathon.db").parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    try:
        insp = inspect(engine)
        if "users" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("users")}
            if "photo" not in cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN photo TEXT"))
        if "device_links" in insp.get_table_names():
            link_cols = {c["name"] for c in insp.get_columns("device_links")}
            if "nickname" not in link_cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE device_links ADD COLUMN nickname VARCHAR(64)"))
        ensure_empire_cash_column(engine)
        ensure_empire_fare_column(engine)
        ensure_empire_loiter_columns(engine)
        ensure_empire_job_column(engine)
        ensure_empire_firm_columns(engine)
        ensure_empire_person_drop_columns(engine)
        ensure_empire_person_arrived_column(engine)
    except Exception:
        pass
    try:
        from .models import ShopAdmin

        founder = os.getenv("KATIE_ADMIN_USERNAME", "LewisThomson")
        ShopAdmin.ensure(founder, founder=True)
    except Exception:
        pass
    try:
        from .board_store import ensure_board_schema, seed_if_empty

        ensure_board_schema()
        if seed_if_empty():
            log.info("Board seeded from app/board_seed.json")
    except Exception as exc:
        log.warning("Board seed skipped: %s", exc)
