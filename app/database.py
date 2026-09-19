"""SQLAlchemy engine and session for the local SQLite file."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

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
    except Exception:
        pass
