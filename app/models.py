"""SQLAlchemy tables and the User helper used by auth."""
from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, PersistError, SessionLocal, UserExistsError, normalise_username, utc_now
from .schemas import Token, User as UserPublic, UserCreate
from .security import (
    DUMMY_HASH,
    access_token_expires,
    create_access_token,
    get_password_hash,
    verify_password,
)


class FieldRecord(Base):
    __tablename__ = "fields"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[str] = mapped_column(String(64), default="")


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_lat: Mapped[float] = mapped_column(Float)
    last_lng: Mapped[float] = mapped_column(Float)
    last_seen_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LocationPing(Base):
    __tablename__ = "location_pings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    speed_mps: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    received_at: Mapped[str | None] = mapped_column(String(64), nullable=True)


class BusCache(Base):
    __tablename__ = "bus_cache"

    region: Mapped[str] = mapped_column(String(64), primary_key=True)
    fetched_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    vehicles_json: Mapped[str] = mapped_column(Text, default="[]")
    trails_json: Mapped[str] = mapped_column(Text, default="{}")


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    hashed_password: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    @classmethod
    def get(cls, username: str) -> User | None:
        key = normalise_username(username)
        if not key:
            return None
        with SessionLocal() as session:
            return session.get(cls, key)

    @classmethod
    def create(cls, payload: UserCreate) -> User:
        user = cls(
            username=normalise_username(payload.username),
            email=payload.email,
            full_name=payload.full_name,
            disabled=False,
            hashed_password=get_password_hash(payload.password),
            updated_at=utc_now(),
        )
        user.save(create_only=True)
        return user

    @classmethod
    def authenticate(cls, username: str, password: str) -> User | None:
        user = cls.get(username)
        if not user:
            verify_password(password, DUMMY_HASH)
            return None
        if not user.check_password(password):
            return None
        return user

    def check_password(self, password: str) -> bool:
        return verify_password(password, self.hashed_password)

    def save(self, *, create_only: bool = False) -> None:
        self.username = normalise_username(self.username)
        self.updated_at = utc_now()
        with SessionLocal() as session:
            try:
                if create_only:
                    session.add(self)
                else:
                    session.merge(self)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if create_only:
                    raise UserExistsError("Username already registered") from exc
                raise PersistError("Could not store user") from exc
            except Exception as exc:
                session.rollback()
                raise PersistError("Could not store user") from exc

    def public(self) -> UserPublic:
        return UserPublic(
            username=self.username,
            email=self.email,
            full_name=self.full_name,
            disabled=self.disabled,
        )

    def issue_token(self) -> Token:
        return Token(
            access_token=create_access_token(
                data={"sub": self.username},
                expires_delta=access_token_expires(),
            ),
            token_type="bearer",
        )
